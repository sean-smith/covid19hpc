from aws_cdk import (
    aws_s3 as s3,
    aws_s3_assets as assets,
    aws_cloud9 as cloud9,
    aws_ec2 as ec2,
    aws_cloudtrail as cloudtrail,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_cloudformation as cfn,
    aws_secretsmanager as secretsmanager,
    aws_budgets as budgets,
    custom_resources as cr,
    core as cdk,
    region_info
)
import json

class PclusterStack(cdk.Stack):

    def __init__(self, scope: cdk.Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Password
        password = cdk.CfnParameter(self, 'UserPasswordParameter', description='Set a password for the hpc-quickstart user', no_echo=True)

        # create a VPC
        vpc = ec2.Vpc(self, 'VPC', cidr='10.0.0.0/20', max_azs=99)

        # create a private and public subnet per vpc
        selection = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE
        )

        # Output created subnets
        for i, public_subnet in enumerate(vpc.public_subnets):
            cdk.CfnOutput(self, 'PublicSubnet%i' % i,  value=public_subnet.subnet_id)

        for i, private_subnet in enumerate(vpc.private_subnets):
            cdk.CfnOutput(self, 'PrivateSubnet%i' % i,  value=private_subnet.subnet_id)

        cdk.CfnOutput(self, 'VPCId',  value=vpc.vpc_id)

        # Create a Bucket
        data_bucket = s3.Bucket(self, "DataRepository")
        cloudtrail_bucket = s3.Bucket(self, "CloudTrailLogs")
        quickstart_bucket = s3.Bucket.from_bucket_name(self, 'QuickStartBucket', 'aws-quickstart')

        # Upload Bootstrap Script to that bucket
        bootstrap_script = assets.Asset(self, 'BootstrapScript',
            path='scripts/bootstrap.sh'
        )

        # Upload parallel cluster post_install_script to that bucket
        pcluster_post_install_script = assets.Asset(self, 'PclusterPostInstallScript',
            path='scripts/post_install_script.sh'
        )

        # Setup CloudTrail
        cloudtrail.Trail(self, 'CloudTrail', bucket=cloudtrail_bucket)

        # Create a Cloud9 instance
        # Cloud9 doesn't have the ability to provide userdata
        # Because of this we need to use SSM run command
        cloud9_instance = cloud9.Ec2Environment(self, 'Cloud9Env', ec2_environment_name='ResearchWorkspace', vpc=vpc, instance_type=ec2.InstanceType(instance_type_identifier='c5.large'))
        cdk.CfnOutput(self, 'Research Workspace URL',  value=cloud9_instance.ide_url)


        # Create a keypair in lambda and store the private key in SecretsManager
        c9_createkeypair_role = iam.Role(self, 'Cloud9CreateKeypairRole', assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'))
        c9_createkeypair_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AWSLambdaBasicExecutionRole'))
        # Add IAM permissions to the lambda role
        c9_createkeypair_role.add_to_policy(iam.PolicyStatement(
            actions=[
                'ec2:CreateKeyPair',
                'ec2:DeleteKeyPair'
            ],
            resources=['*'],
        ))

        # Lambda for Cloud9 keypair
        c9_createkeypair_lambda = _lambda.Function(self, 'C9CreateKeyPairLambda',
            runtime=_lambda.Runtime.PYTHON_3_6,
            handler='lambda_function.handler',
            timeout=cdk.Duration.seconds(300),
            role=c9_createkeypair_role,
            code=_lambda.Code.asset('functions/source/c9keypair'),
        )

        c9_createkeypair_provider = cr.Provider(self, "C9CreateKeyPairProvider", on_event_handler=c9_createkeypair_lambda)

        c9_createkeypair_cr = cfn.CustomResource(self, "C9CreateKeyPair", provider=c9_createkeypair_provider,
            properties={
                'ServiceToken': c9_createkeypair_lambda.function_arn
            }
        )
        #c9_createkeypair_cr.node.add_dependency(instance_id)
        c9_ssh_private_key_secret = secretsmanager.CfnSecret(self, 'SshPrivateKeySecret',
             secret_string=c9_createkeypair_cr.get_att_string('PrivateKey')
        )

        # The iam policy has a <REGION> parameter that needs to be replaced.
        # We do it programmatically so future versions of the synth'd stack
        # template include all regions.
        with open('iam/ParallelClusterUserPolicy.json') as json_file:
            data = json.load(json_file)
            for s in data['Statement']:
                if s['Sid'] == 'S3ParallelClusterReadOnly':
                    s['Resource'] = []
                    for r in region_info.RegionInfo.regions:
                        s['Resource'].append('arn:aws:s3:::{0}-aws-parallelcluster*'.format(r.name))

            parallelcluster_user_policy = iam.CfnManagedPolicy(self, 'ParallelClusterUserPolicy', policy_document=iam.PolicyDocument.from_json(data))

        # Cloud9 IAM Role
        cloud9_role = iam.Role(self, 'Cloud9Role', assumed_by=iam.ServicePrincipal('ec2.amazonaws.com'))
        cloud9_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AmazonSSMManagedInstanceCore'))
        cloud9_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AWSCloud9User'))
        cloud9_role.add_managed_policy(iam.ManagedPolicy.from_managed_policy_arn(self, 'AttachParallelClusterUserPolicy', parallelcluster_user_policy.ref))
        cloud9_role.add_to_policy(iam.PolicyStatement(
            resources=['*'],
            actions=[
                'ec2:DescribeInstances',
                'ec2:DescribeVolumes',
                'ec2:ModifyVolume'
            ]
        ))
        cloud9_role.add_to_policy(iam.PolicyStatement(
            resources=[c9_ssh_private_key_secret.ref],
            actions=[
                'secretsmanager:GetSecretValue'
            ]
        ))
        cloud9_role.add_to_policy(iam.PolicyStatement(
            actions=[
             "s3:Get*",
             "s3:List*"
            ],
            resources=[
                "arn:aws:s3:::%s/*" % (data_bucket.bucket_name),
                "arn:aws:s3:::%s" % (data_bucket.bucket_name)
            ]
        ))

        bootstrap_script.grant_read(cloud9_role)
        pcluster_post_install_script.grant_read(cloud9_role)

        # Admin Group
        admin_group = iam.Group(self, 'AdminGroup')
        admin_group.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AdministratorAccess'))
        admin_group.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AWSCloud9Administrator'))

        # PowerUser Group
        poweruser_group = iam.Group(self, 'PowerUserGroup')
        poweruser_group.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('PowerUserAccess'))
        poweruser_group.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AWSCloud9Administrator'))

        # HPC User
        user_name = 'hpc-quickstart'
        user = iam.User(self, 'Researcher',
                        #user_name=user_name,
                        password=cdk.SecretValue.cfn_parameter(password), password_reset_required=True)
        user.add_to_group(group=admin_group)
        cdk.CfnOutput(self, 'UserLoginUrl', value="".join(["https://", self.account,".signin.aws.amazon.com/console"]))
        cdk.CfnOutput(self, 'UserName', value=user.user_name)
        # cdk.CfnOutput(self, 'UserPassword', value=password.value_as_string)

        # Cloud9 Setup IAM Role
        cloud9_setup_role = iam.Role(self, 'Cloud9SetupRole', assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'))
        cloud9_setup_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AWSLambdaBasicExecutionRole'))
        # Allow pcluster to be run in bootstrap
        cloud9_setup_role.add_managed_policy(iam.ManagedPolicy.from_managed_policy_arn(self, 'AttachParallelClusterUserPolicySetup', parallelcluster_user_policy.ref))

        # Add IAM permissions to the lambda role
        cloud9_setup_role.add_to_policy(iam.PolicyStatement(
            actions=[
                'cloudformation:DescribeStackResources',
                'ec2:AssociateIamInstanceProfile',
                'ec2:AuthorizeSecurityGroupIngress',
                'ec2:DescribeInstances',
                'ec2:DescribeInstanceStatus',
                'ec2:DescribeInstanceAttribute',
                'ec2:DescribeIamInstanceProfileAssociations',
                'ec2:DescribeVolumes',
                'ec2:DesctibeVolumeAttribute',
                'ec2:DescribeVolumesModifications',
                'ec2:DescribeVolumeStatus',
                'ssm:DescribeInstanceInformation',
                'ec2:ModifyVolume',
                'ec2:ReplaceIamInstanceProfileAssociation',
                'ec2:ReportInstanceStatus',
                'ssm:SendCommand',
                'ssm:GetCommandInvocation',
                's3:GetObject',
                'lambda:AddPermission',
                'lambda:RemovePermission',
                'events:PutRule',
                'events:DeleteRule',
                'events:PutTargets',
                'events:RemoveTargets',
                'cloud9:CreateEnvironmentMembership',
            ],
            resources=['*'],
        ))

        cloud9_setup_role.add_to_policy(iam.PolicyStatement(
            actions=['iam:PassRole'],
            resources=[cloud9_role.role_arn]
        ))

        cloud9_setup_role.add_to_policy(iam.PolicyStatement(
            actions=[
                'lambda:AddPermission',
                'lambda:RemovePermission'
            ],
            resources=['*']
        ))

        # Cloud9 Instance Profile
        c9_instance_profile = iam.CfnInstanceProfile(self, "Cloud9InstanceProfile", roles=[cloud9_role.role_name])

        # Lambda to add Instance Profile to Cloud9
        c9_instance_profile_lambda = _lambda.Function(self, 'C9InstanceProfileLambda',
            runtime=_lambda.Runtime.PYTHON_3_6,
            handler='lambda_function.handler',
            timeout=cdk.Duration.seconds(900),
            role=cloud9_setup_role,
            code=_lambda.Code.asset('functions/source/c9InstanceProfile'),
        )

        c9_instance_profile_provider = cr.Provider(self, "C9InstanceProfileProvider",
            on_event_handler=c9_instance_profile_lambda,
        )

        instance_id = cfn.CustomResource(self, "C9InstanceProfile", provider=c9_instance_profile_provider,
            properties={
                'InstanceProfile': c9_instance_profile.ref,
                'Cloud9Environment': cloud9_instance.environment_id,
            }
        )
        instance_id.node.add_dependency(cloud9_instance)

        # Lambda for Cloud9 Bootstrap
        c9_bootstrap_lambda = _lambda.Function(self, 'C9BootstrapLambda',
            runtime=_lambda.Runtime.PYTHON_3_6,
            handler='lambda_function.handler',
            timeout=cdk.Duration.seconds(900),
            role=cloud9_setup_role,
            code=_lambda.Code.asset('functions/source/c9bootstrap'),
        )

        c9_bootstrap_provider = cr.Provider(self, "C9BootstrapProvider", on_event_handler=c9_bootstrap_lambda)

        c9_bootstrap_cr = cfn.CustomResource(self, "C9Bootstrap", provider=c9_bootstrap_provider,
            properties={
                'Cloud9Environment': cloud9_instance.environment_id,
                'BootstrapPath': 's3://%s/%s' % (bootstrap_script.s3_bucket_name, bootstrap_script.s3_object_key),
                'VPCID': vpc.vpc_id,
                'MasterSubnetID': vpc.public_subnets[0].subnet_id,
                'ComputeSubnetID': vpc.private_subnets[0].subnet_id,
                'PostInstallScriptS3Url':  "".join( ['s3://', pcluster_post_install_script.s3_bucket_name,  "/", pcluster_post_install_script.s3_object_key ] ),
                'PostInstallScriptBucket': pcluster_post_install_script.s3_bucket_name,
                'S3ReadWriteResource': data_bucket.bucket_arn,
                'S3ReadWriteUrl': 's3://%s' % ( data_bucket.bucket_name ),
                'KeyPairId':  c9_createkeypair_cr.ref,
                'KeyPairSecretArn': c9_ssh_private_key_secret.ref,
                'UserArn': user.user_arn
            }
        )
        c9_bootstrap_cr.node.add_dependency(instance_id)
        c9_bootstrap_cr.node.add_dependency(c9_createkeypair_cr)
        c9_bootstrap_cr.node.add_dependency(c9_ssh_private_key_secret)
        c9_bootstrap_cr.node.add_dependency(data_bucket)

        # Budgets
        budget_properties = {
            'budgetType': "COST",
            'timeUnit': "ANNUALLY",
            'budgetLimit': {
                'amount': cdk.CfnParameter(self, 'BudgetLimit', description='The initial budget for this project in USD ($).', default=2000, type='Number').value_as_number,
                'unit': "USD",
            },
            'costFilters': None,
            'costTypes': {
                'includeCredit': False,
                'includeDiscount': True,
                'includeOtherSubscription': True,
                'includeRecurring': True,
                'includeRefund': True,
                'includeSubscription': True,
                'includeSupport': True,
                'includeTax': True,
                'includeUpfront': True,
                'useAmortized': False,
                'useBlended': False,
            },
            'plannedBudgetLimits': None,
            'timePeriod': None,
        }

        stesachs = {
            'notification': {
                'comparisonOperator': "GREATER_THAN",
                'notificationType': "ACTUAL",
                'threshold': 80,
                'thresholdType': "PERCENTAGE",
                },
            'subscribers': [{
                'address': cdk.CfnParameter(self, 'NotificationEmail', description='This email address will receive billing alarm notifications when 80% of the budget limit is reached.', default='stesachs@amazon.com').value_as_string,
                'subscriptionType': "EMAIL",
            }]
        }

        overall_budget = budgets.CfnBudget(
            self,
            "CovidHPCBudget",
            budget=budget_properties,
            notifications_with_subscribers=[stesachs],
        )

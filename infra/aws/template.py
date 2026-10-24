"""Render the secret-free CloudFormation template (no AWS calls)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ref = lambda name: {"Ref": name}
sub = lambda value: {"Fn::Sub": value}
att = lambda resource, name: {"Fn::GetAtt": [resource, name]}


def template():
    resources = {
        "Vpc": {"Type": "AWS::EC2::VPC", "Properties": {"CidrBlock": "10.42.0.0/16", "EnableDnsHostnames": True, "EnableDnsSupport": True}},
        "InternetGateway": {"Type": "AWS::EC2::InternetGateway"},
        "GatewayAttachment": {"Type": "AWS::EC2::VPCGatewayAttachment", "Properties": {"VpcId": ref("Vpc"), "InternetGatewayId": ref("InternetGateway")}},
        "Subnet": {"Type": "AWS::EC2::Subnet", "Properties": {"VpcId": ref("Vpc"), "CidrBlock": "10.42.1.0/24", "MapPublicIpOnLaunch": True, "AvailabilityZone": {"Fn::Select": [0, {"Fn::GetAZs": ""}]}}},
        "RouteTable": {"Type": "AWS::EC2::RouteTable", "Properties": {"VpcId": ref("Vpc")}},
        "InternetRoute": {"Type": "AWS::EC2::Route", "DependsOn": "GatewayAttachment", "Properties": {"RouteTableId": ref("RouteTable"), "DestinationCidrBlock": "0.0.0.0/0", "GatewayId": ref("InternetGateway")}},
        "RouteAssociation": {"Type": "AWS::EC2::SubnetRouteTableAssociation", "Properties": {"SubnetId": ref("Subnet"), "RouteTableId": ref("RouteTable")}},
        "SecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {"GroupDescription": "Stova HTTP origin only from CloudFront; no SSH, database, or API ingress", "VpcId": ref("Vpc"), "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "SourcePrefixListId": ref("CloudFrontPrefixListId")}], "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}]}},
        "ReleaseBucket": {"Type": "AWS::S3::Bucket", "Properties": {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}, "BucketEncryption": {"ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}, "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}, "LifecycleConfiguration": {"Rules": [{"Id": "ExpireOldReleases", "Status": "Enabled", "Prefix": "releases/", "ExpirationInDays": 30}, {"Id": "AbortIncompleteUploads", "Status": "Enabled", "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}}},
        "BucketPolicy": {"Type": "AWS::S3::BucketPolicy", "Properties": {"Bucket": ref("ReleaseBucket"), "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [att("ReleaseBucket", "Arn"), sub("${ReleaseBucket.Arn}/*")], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}}},
        "HostRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "RuntimeAndSessionManager", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["ssm:UpdateInstanceInformation", "ssm:ListInstanceAssociations", "ssm:DescribeAssociation", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:GetDocument", "ssm:DescribeDocument", "ssm:GetManifest", "ssm:PutInventory", "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult", "ssm:ListAssociations", "ssm:UpdateAssociationStatus", "ssm:UpdateInstanceAssociationStatus"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"], "Resource": sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/stova/${AWS::StackName}/runtime")},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": sub("${ReleaseBucket.Arn}/*")},
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": sub("${ReleaseBucket.Arn}/backups/*")},
            {"Effect": "Allow", "Action": ["freetier:GetAccountPlanState"], "Resource": "*"}
        ]}}]}},
        "HostProfile": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [ref("HostRole")]}},
        "Instance": {"Type": "AWS::EC2::Instance", "DependsOn": ["InternetRoute", "RouteAssociation"], "Properties": {"ImageId": ref("LatestAmiId"), "InstanceType": "t3.micro", "CreditSpecification": {"CPUCredits": "standard"}, "IamInstanceProfile": ref("HostProfile"), "InstanceInitiatedShutdownBehavior": "stop", "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "InstanceMetadataTags": "disabled"}, "NetworkInterfaces": [{"DeviceIndex": "0", "SubnetId": ref("Subnet"), "AssociatePublicIpAddress": True, "GroupSet": [ref("SecurityGroup")]}], "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 20, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}], "Tags": [{"Key": "Name", "Value": sub("${AWS::StackName}-stova")}, {"Key": "Application", "Value": "Stova"}], "UserData": {"Fn::Base64": (HERE / "bootstrap.sh").read_text()}}},
        "Distribution": {"Type": "AWS::CloudFront::Distribution", "Properties": {"DistributionConfig": {"Enabled": False, "Comment": sub("Stova ${AWS::StackName}; secret activated by the deployment script"), "HttpVersion": "http2and3", "IPV6Enabled": True, "PriceClass": "PriceClass_100", "Origins": [{"Id": "stova-origin", "DomainName": att("Instance", "PublicDnsName"), "CustomOriginConfig": {"HTTPPort": 8080, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only", "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}}], "DefaultCacheBehavior": {"TargetOriginId": "stova-origin", "ViewerProtocolPolicy": "redirect-to-https", "AllowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"], "CachedMethods": ["GET", "HEAD", "OPTIONS"], "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad", "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3", "Compress": False}, "ViewerCertificate": {"CloudFrontDefaultCertificate": True}}}},
        "GitHubProvider": {"Type": "AWS::IAM::OIDCProvider", "Condition": "CreateGitHubProvider", "Properties": {"Url": "https://token.actions.githubusercontent.com", "ClientIdList": ["sts.amazonaws.com"]}},
        "GitHubRole": {"Type": "AWS::IAM::Role", "Condition": "GitHubEnabled", "Properties": {"MaxSessionDuration": 3600, "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Federated": {"Fn::If": ["CreateGitHubProvider", ref("GitHubProvider"), ref("ExistingGitHubOidcArn")]}}, "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com", "token.actions.githubusercontent.com:sub": sub("repo:${GitHubRepo}:ref:refs/heads/main")}}}]}, "Policies": [{"PolicyName": "ReleaseOnly", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["freetier:GetAccountPlanState"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["cloudformation:DescribeStacks"], "Resource": ref("AWS::StackId")},
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": [sub("${ReleaseBucket.Arn}/releases/*"), sub("${ReleaseBucket.Arn}/bootstrap/*")]},
            {"Effect": "Allow", "Action": ["ssm:SendCommand"], "Resource": [sub("arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${Instance}"), sub("arn:${AWS::Partition}:ssm:${AWS::Region}::document/AWS-RunShellScript")]},
            {"Effect": "Allow", "Action": ["ssm:GetCommandInvocation"], "Resource": "*"}
        ]}}]}}
    }
    return {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Stova credit-backed Free-account deployment; no permanent-free guarantee. Secret values are never CloudFormation parameters.", "Parameters": {"CloudFrontPrefixListId": {"Type": "String", "AllowedPattern": "pl-[a-f0-9]+"}, "LatestAmiId": {"Type": "AWS::EC2::Image::Id", "Description": "Pinned AL2023 x86_64 AMI; script resolves the public SSM parameter only on first creation"}, "GitHubRepo": {"Type": "String", "Default": "", "AllowedPattern": "^$|^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"}, "ExistingGitHubOidcArn": {"Type": "String", "Default": ""}}, "Conditions": {"GitHubEnabled": {"Fn::Not": [{"Fn::Equals": [ref("GitHubRepo"), ""]}]}, "CreateGitHubProvider": {"Fn::And": [{"Fn::Not": [{"Fn::Equals": [ref("GitHubRepo"), ""]}]}, {"Fn::Equals": [ref("ExistingGitHubOidcArn"), ""]}]}}, "Resources": resources, "Outputs": {"ReleaseBucket": {"Value": ref("ReleaseBucket")}, "InstanceId": {"Value": ref("Instance")}, "DistributionId": {"Value": ref("Distribution")}, "Url": {"Value": sub("https://${Distribution.DomainName}")}, "GitHubRoleArn": {"Condition": "GitHubEnabled", "Value": att("GitHubRole", "Arn")}, "RuntimeParameter": {"Value": sub("/stova/${AWS::StackName}/runtime")}}}


if __name__ == "__main__":
    (HERE / "template.json").write_text(json.dumps(template(), indent=2) + "\n")

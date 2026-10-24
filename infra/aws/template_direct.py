"""Render the separate direct-HTTPS CloudFormation template without AWS calls.

The shared template remains the source for compute, private release storage,
Session Manager, and narrowly scoped GitHub identity permissions. Direct hosting
changes only the public edge and its bootstrap script. Deployment eligibility,
TLS activation, and owner secrets are handled separately by the guarded CLI.
"""
from copy import deepcopy
import json
from pathlib import Path

if __package__:
    from .template import template as base_template
else:
    from template import template as base_template

HERE = Path(__file__).resolve().parent


def template():
    result = deepcopy(base_template())
    result["Description"] = (
        "Stova direct HTTPS deployment on a credit-backed Free account; no permanent-free guarantee. "
        "Secret values are never CloudFormation parameters."
    )
    result["Parameters"].pop("CloudFrontPrefixListId")
    resources = result["Resources"]
    resources.pop("Distribution")
    resources["SecurityGroup"]["Properties"].update({
        "GroupDescription": "Stova direct HTTPS and ACME HTTP only; no SSH, database, or application ingress",
        "SecurityGroupIngress": [
            {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "CidrIp": "0.0.0.0/0"}
            for port in (80, 443)
        ],
    })
    resources["Instance"]["Properties"]["UserData"] = {
        "Fn::Base64": (HERE / "bootstrap.direct.sh").read_text(encoding="utf-8"),
    }
    # The instance may bootstrap with its temporary public address. This stable
    # address replaces it after association; there is no second retained address.
    resources["PublicAddress"] = {
        "Type": "AWS::EC2::EIP",
        "DependsOn": "GatewayAttachment",
        "Properties": {"Domain": "vpc", "Tags": [
            {"Key": "Name", "Value": {"Fn::Sub": "${AWS::StackName}-stova"}},
            {"Key": "Application", "Value": "Stova"},
        ]},
    }
    resources["PublicAddressAssociation"] = {
        "Type": "AWS::EC2::EIPAssociation",
        "Properties": {
            "AllocationId": {"Fn::GetAtt": ["PublicAddress", "AllocationId"]},
            "InstanceId": {"Ref": "Instance"},
        },
    }
    outputs = result["Outputs"]
    outputs.pop("DistributionId")
    outputs.update({
        "HostingMode": {"Value": "direct"},
        "Hostname": {"Value": {"Fn::Sub": "stova.${PublicAddress}.sslip.io"}},
        "Url": {"Value": {"Fn::Sub": "https://stova.${PublicAddress}.sslip.io"}},
        "PublicAddress": {"Value": {"Ref": "PublicAddress"}},
    })
    return result


if __name__ == "__main__":
    (HERE / "template.direct.json").write_text(json.dumps(template(), indent=2) + "\n", encoding="utf-8")

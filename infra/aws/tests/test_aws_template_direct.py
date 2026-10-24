"""Offline direct-hosting invariants. Rendering and tests make no AWS calls."""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from infra.aws import template as shared
from infra.aws import template_direct as direct


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class DirectTemplateTests(unittest.TestCase):
    def setUp(self):
        self.common = shared.template()
        self.direct = direct.template()

    def test_shared_template_and_generated_file_are_not_mutated(self):
        snapshot = deepcopy(self.common)
        direct.template()
        self.assertEqual(shared.template(), snapshot)
        generated = json.loads((direct.HERE / "template.direct.json").read_text(encoding="utf-8"))
        self.assertEqual(generated, self.direct)
        original = json.loads((shared.HERE / "template.json").read_text(encoding="utf-8"))
        self.assertEqual(original, self.common)

    def test_no_distribution_or_prefix_parameter_or_dangling_reference(self):
        self.assertEqual(set(self.direct["Parameters"]), {"LatestAmiId", "GitHubRepo", "ExistingGitHubOidcArn"})
        self.assertEqual(self.direct["Conditions"], self.common["Conditions"])
        self.assertNotIn("Distribution", self.direct["Resources"])
        self.assertNotIn("DistributionId", self.direct["Outputs"])
        for value in walk(self.direct):
            if isinstance(value, dict):
                if "Ref" in value:
                    target = value["Ref"]
                    self.assertNotIn(target, {"Distribution", "CloudFrontPrefixListId"})
                    self.assertTrue(target in self.direct["Resources"] or target in self.direct["Parameters"] or target.startswith("AWS::"), target)
                if "Fn::GetAtt" in value:
                    target = value["Fn::GetAtt"][0]
                    self.assertIn(target, self.direct["Resources"])
                if "Fn::If" in value:
                    self.assertIn(value["Fn::If"][0], self.direct["Conditions"])
                if "Condition" in value and isinstance(value["Condition"], str):
                    self.assertIn(value["Condition"], self.direct["Conditions"])
                if "DependsOn" in value:
                    dependencies = value["DependsOn"]
                    for target in dependencies if isinstance(dependencies, list) else [dependencies]:
                        self.assertIn(target, self.direct["Resources"])
                if "Fn::Sub" in value:
                    expression = value["Fn::Sub"]
                    text, locals_ = (expression, {}) if isinstance(expression, str) else expression
                    for token in re.findall(r"\$\{([^}]+)\}", text):
                        target = token.split(".")[0]
                        self.assertTrue(target.startswith("AWS::") or target in self.direct["Resources"] or target in self.direct["Parameters"] or target in locals_, token)
                if "Type" in value:
                    self.assertFalse(value["Type"].startswith("AWS::CloudFront::"))

    def test_only_http_and_https_are_public_and_no_key_pair_exists(self):
        properties = self.direct["Resources"]["SecurityGroup"]["Properties"]
        self.assertEqual(properties["SecurityGroupIngress"], [
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "CidrIp": "0.0.0.0/0"},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"},
        ])
        self.assertNotIn("KeyName", self.direct["Resources"]["Instance"]["Properties"])
        self.assertFalse(any(resource["Type"] == "AWS::EC2::SecurityGroupIngress" for resource in self.direct["Resources"].values()))

    def test_one_vpc_eip_is_stack_owned_and_associated_to_exact_instance(self):
        resources = self.direct["Resources"]
        self.assertEqual([name for name, resource in resources.items() if resource["Type"] == "AWS::EC2::EIP"], ["PublicAddress"])
        self.assertEqual(resources["PublicAddress"]["Properties"]["Domain"], "vpc")
        self.assertEqual(resources["PublicAddress"]["DependsOn"], "GatewayAttachment")
        self.assertNotIn("DeletionPolicy", resources["PublicAddress"])
        association = resources["PublicAddressAssociation"]
        self.assertEqual(association["Type"], "AWS::EC2::EIPAssociation")
        self.assertEqual(association["Properties"], {
            "AllocationId": {"Fn::GetAtt": ["PublicAddress", "AllocationId"]}, "InstanceId": {"Ref": "Instance"},
        })
        self.assertEqual(resources["Instance"]["Properties"]["NetworkInterfaces"], self.common["Resources"]["Instance"]["Properties"]["NetworkInterfaces"])

    def test_instance_retains_standard_micro_encrypted_disk_and_imdsv2(self):
        instance = self.direct["Resources"]["Instance"]["Properties"]
        self.assertEqual(instance["InstanceType"], "t3.micro")
        self.assertEqual(instance["CreditSpecification"], {"CPUCredits": "standard"})
        self.assertEqual(instance["ImageId"], {"Ref": "LatestAmiId"})
        self.assertEqual(instance["MetadataOptions"]["HttpTokens"], "required")
        self.assertEqual(instance["InstanceInitiatedShutdownBehavior"], "stop")
        self.assertEqual(instance["BlockDeviceMappings"], [{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": 20, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True,
        }}])
        self.assertEqual(instance["UserData"], {"Fn::Base64": (direct.HERE / "bootstrap.direct.sh").read_text(encoding="utf-8")})
        self.assertLessEqual(len(instance["UserData"]["Fn::Base64"].encode("utf-8")), 16_384)
        before, after = deepcopy(self.common["Resources"]["Instance"]), deepcopy(self.direct["Resources"]["Instance"])
        before["Properties"].pop("UserData")
        after["Properties"].pop("UserData")
        self.assertEqual(before, after)

    def test_storage_iam_ssm_github_and_network_are_reused_without_expansion(self):
        changed = {"Distribution", "SecurityGroup", "Instance"}
        for name, resource in self.common["Resources"].items():
            if name not in changed:
                self.assertEqual(self.direct["Resources"][name], resource, name)
        self.assertEqual(set(self.direct["Resources"]) - set(self.common["Resources"]), {"PublicAddress", "PublicAddressAssociation"})
        host_statements = self.direct["Resources"]["HostRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertTrue(any("freetier:GetAccountPlanState" in statement["Action"] for statement in host_statements))
        bucket = self.direct["Resources"]["ReleaseBucket"]["Properties"]
        self.assertTrue(all(bucket["PublicAccessBlockConfiguration"].values()))
        self.assertNotIn("AWS::Route53::HostedZone", [resource["Type"] for resource in self.direct["Resources"].values()])

    def test_direct_hostname_and_url_use_the_stable_eip_and_normal_outputs_remain(self):
        outputs = self.direct["Outputs"]
        self.assertEqual(outputs["HostingMode"], {"Value": "direct"})
        self.assertEqual(outputs["Hostname"], {"Value": {"Fn::Sub": "stova.${PublicAddress}.sslip.io"}})
        self.assertEqual(outputs["Url"], {"Value": {"Fn::Sub": "https://stova.${PublicAddress}.sslip.io"}})
        self.assertEqual(outputs["PublicAddress"], {"Value": {"Ref": "PublicAddress"}})
        for key in ("ReleaseBucket", "InstanceId", "GitHubRoleArn", "RuntimeParameter"):
            self.assertEqual(outputs[key], self.common["Outputs"][key])

    def test_missing_direct_bootstrap_fails_instead_of_emitting_placeholder_userdata(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(direct, "HERE", Path(folder)):
            with self.assertRaises(FileNotFoundError):
                direct.template()


if __name__ == "__main__":
    unittest.main()

"""
Wrapper to connect resources to lambda calls.
"""
from pathlib import Path
import os

import pulumi
from pulumi_aws import (
    s3, lambda_, elasticloadbalancingv2 as elb, ec2, iam
)

from putils import (
    opts, Component, component, outputish, get_region, Certificate, a_aaaa,
    get_public_subnets,
)

from .builders.pipenv import PipenvPackage
from .resourcegen import ResourceGenerator
from .rolegen import generate_role

__all__ = 'Package', 'EventHandler', 'AwsgiHandler',

# Requirements:
#  * EventHandler(resource, event, package, func)
#    - Wires up the function (in the package) to an event on a specific resource
#  * Package(sourcedir, resources)
#    - Does pipenv-based dependencies
#    - Manages the build process to produce a bundle for lambda
#    - A single package may contain multiple bundles
#    - Generates roles to access the given resources
#    - Generates code in the package to instantiate resources.

_lambda_buckets = {}


def get_lambda_bucket(region=None, resource=None):
    """
    Gets the shared bucket for lambda packages for the given region
    """
    if resource is not None:
        region = get_region(resource)

    if region not in _lambda_buckets:
        _lambda_buckets[region] = s3.Bucket(
            f'lambda-bucket-{region}',
            region=region,
            versioning={
                'enabled': True,
            },
            # FIXME: Life cycle rules for expiration
            **opts(region=region),
        )

    return _lambda_buckets[region]


@outputish
async def build_zip_package(sourcedir, resgen):
    sourcedir = Path(sourcedir)
    if (sourcedir / 'Pipfile').is_file():
        package = PipenvPackage(sourcedir, resgen)
    else:
        raise OSError("Unable to detect package type")

    # Do any preparatory stuff
    await package.warmup()

    # Actually build the zip
    bundle = await package.build()

    return pulumi.FileAsset(os.fspath(bundle))


class Package(Component, outputs=['funcargs', 'bucket', 'object', 'role', '_resources']):
    def set_up(self, name, *, sourcedir, resources=None, __opts__):
        if resources is None:
            resources = {}
        resgen = ResourceGenerator(resources)
        bucket = get_lambda_bucket(resource=self)
        bobj = s3.BucketObject(
            f'{name}-code',
            bucket=bucket.id,
            source=build_zip_package(sourcedir, resgen),
            **opts(parent=self),
        )

        role = generate_role(
            f'{name}-role',
            {
                rname: (res, ...)  # Ask for basic RW permissions (not manage)
                for rname, res in resources.items()
            },
            **opts(parent=self)
        )

        return {
            'bucket': bucket,
            'object': bobj,
            'role': role,
            '_resources': list(resources.values()),  # This should only be used internally
        }

    def function(self, name, func, **kwargs):
        func = func.replace(':', '.')
        function = lambda_.Function(
            f'{name}',
            handler=func,
            s3_bucket=self.bucket.bucket,
            s3_key=self.object.key,
            s3_object_version=self.object.version_id,
            runtime='python3.7',
            role=self.role.arn,
            **kwargs,
        )
        return function


@component(outputs=[])
def EventHandler(self, name, resource, event, package, func, __opts__):
    """
    Define a handler to process an event produced by a resource
    """
    ...


# FIXME: This isn't working with IPv6 (timing out)
@component(outputs=[])
def AwsgiHandler(self, name, zone, domain, package, func, __opts__, **lambdaargs):
    """
    Define a handler to accept requests, using awsgi
    """
    func = package.function(f"{name}-function", func, **lambdaargs, **opts(parent=self))

    invoke_policy = lambda_.Permission(
        f'{name}-function-permission',
        function=func,
        action='lambda:InvokeFunction',
        principal='elasticloadbalancing.amazonaws.com',
        **opts(parent=func)
    )

    netinfo = get_public_subnets(opts=__opts__)

    @netinfo.apply
    def vpc_id(info):
        vpc, subnets, is_v6 = info
        return vpc.id

    @netinfo.apply
    def netstack(info):
        vpc, subnets, is_v6 = info
        return 'dualstack' if is_v6 else 'ipv4'

    @netinfo.apply
    def subnet_ids(info):
        vpc, subnets, is_v6 = info
        return [sn.id for sn in subnets]

    cert = Certificate(
        f"{name}-cert",
        domain=domain,
        zone=zone,
        **opts(parent=self)
    )

    # TODO: Cache this
    sg = ec2.SecurityGroup(
        f"{name}-sg",
        vpc_id=vpc_id,
        ingress=[
            {
                'from_port': 80,
                'to_port': 80,
                'protocol': "tcp",
                'cidr_blocks': ['0.0.0.0/0'],
            },
            {
                'from_port': 443,
                'to_port': 443,
                'protocol': "tcp",
                'cidr_blocks': ['0.0.0.0/0'],
            },
            {
                'from_port': 80,
                'to_port': 80,
                'protocol': "tcp",
                'ipv6_cidr_blocks': ['::/0'],
            },
            {
                'from_port': 443,
                'to_port': 443,
                'protocol': "tcp",
                'ipv6_cidr_blocks': ['::/0'],
            },
        ],
        egress=[
            {
                'from_port': 0,
                'to_port': 0,
                'protocol': "-1",
                'cidr_blocks': ['0.0.0.0/0'],
            },
            {
                'from_port': 0,
                'to_port': 0,
                'protocol': "-1",
                'ipv6_cidr_blocks': ['::/0'],
            },
        ],
        **opts(parent=self)
    )

    alb = elb.LoadBalancer(
        f"{name}-alb",
        load_balancer_type='application',
        subnets=subnet_ids,
        ip_address_type=netstack,
        security_groups=[sg],
        enable_http2=True,
        **opts(parent=self)
    )

    target = elb.TargetGroup(
        f"{name}-target",
        target_type='lambda',
        lambda_multi_value_headers_enabled=False,  # AWSGI does not support this yet
        health_check={
            'enabled': True,
            'path': '/',
            'matcher': '200-299',
            'interval': 30,
            'timeout': 5,
        },
        **opts(parent=self)
    )

    elb.TargetGroupAttachment(
        f"{name}-target-func",
        target_group_arn=target.arn,
        target_id=func.arn,
        **opts(depends_on=[invoke_policy], parent=self)
    )

    elb.Listener(
        f"{name}-http",
        load_balancer_arn=alb.arn,
        port=80,
        protocol='HTTP',
        default_actions=[
            {
                'type': 'forward',
                'target_group_arn': target.arn,
            }
        ],
        **opts(parent=self)
    )

    elb.Listener(
        f"{name}-https",
        load_balancer_arn=alb.arn,
        port=443,
        protocol='HTTPS',
        ssl_policy='ELBSecurityPolicy-TLS-1-2-Ext-2018-06',
        certificate_arn=cert.cert_arn,
        default_actions=[
            {
                'type': 'forward',
                'target_group_arn': target.arn,
            }
        ],
        **opts(parent=self)
    )

    a_aaaa(
        f"{name}-record",
        name=domain,
        zone_id=zone.zone_id,
        aliases=[
            {
                'name': alb.dns_name,
                'zone_id': alb.zone_id,
                'evaluate_target_health': True,
            },
        ],
        **opts(parent=self),
    )

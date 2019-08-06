"""
Code to generate AWS IAM data (roles, etc) based on a pile of resources and the
desired actions for each of them.
"""
import pulumi
from pulumi_aws import iam
from putils import opts, FauxOutput

# * Specific list of actions: Just those actions
# * '*': Everything
# * ...: R/W but not manage (reasonable for an application)

# generate_roles(
#     (BufferBucket, '*'),
#     (WorkQueue, ...),
#     InheritedRole,
# )

BASIC_POLICY = FauxOutput(iam.get_policy('arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'))


def generate_role(name, resources, **ropts):
    role = iam.Role(
        f'{name}',
        assume_role_policy={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {
                    "Service": "lambda.amazonaws.com",
                }
              }]
        },
        **ropts,
    )
    iam.RolePolicyAttachment(
        f'{name}-base',
        role=role,
        policy_arn=BASIC_POLICY.arn,
        **opts(parent=role)
    )

    if resources:
        iam.RolePolicy(
            f'{name}-policy',
            role=role,
            policy={
                "Version": "2012-10-17",
                # FIXME: Reduce this
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "*",  # FIXME: More reasonable permissions
                    "Resource": pulumi.Output.all(*[
                        res[0].arn
                        for res in resources.values()
                    ])
                  }]
            },
            **opts(parent=role)
        )

    return role

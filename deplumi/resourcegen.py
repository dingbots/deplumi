import pulumi
import ast

# Knowledge of how to construct a boto3 resource from a pulumi resource
BUILDERS = {
    # full type name: (attrs, expression)
    # Pulumi type name -> attrs on the pulumi object, expression to build a boto3 object
    # The attrs form the namespace of the expression
    'pulumi_aws.s3.bucket.Bucket': (('bucket',), "boto3.resource('s3').Bucket(bucket)"),
}

HEADER = """
import boto3
import functools

_resources = {
"""

# _resources is fully generated
# dict: name:str -> (callable, arguments)
# item name -> the lambda from BUILDERS, the values of the attrs
ITEM = """
    {name!r}: ((lambda {args}: {expr}), {values!r}),
"""

FOOTER = """
}

@functools.lru_cache()
def __getattr__(name):
    if name not in _resources:
        raise AttributeError(f"{name} is not a defined resource")
    ctor, args = _resources[name]
    return ctor(*args)
"""


def get_fqn(obj):
    if not isinstance(obj, type):
        obj = type(obj)
    return f"{obj.__module__}.{obj.__qualname__}"


class ResourceGenerator:
    """
    Generates an __res__.py for packages
    """
    def __init__(self, resources):
        self.resources = resources

    async def build(self):
        contents = HEADER
        for name, res in self.resources.items():
            attrs, expr = BUILDERS[get_fqn(res)]
            values = await pulumi.Output.all(*[
                getattr(res, attr)
                for attr in attrs
            ]).future()
            contents += ITEM.format(
                name=name,
                attrs=attrs,
                expr=expr,
                values=tuple(values),
                args=', '.join(attrs),
            )
        contents += FOOTER

        # Double check we generated valid code
        # This is just a check so that weird errors don't happen at run time
        ast.parse(contents)

        return contents

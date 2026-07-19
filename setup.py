from setuptools import find_namespace_packages, setup

setup(
    # Frappe modules intentionally use PEP 420 namespace directories in several
    # runtime paths. find_packages() silently omitted those modules from wheels,
    # so a source checkout worked while the published candidate was incomplete.
    packages=find_namespace_packages(
        include=["kopos_connector", "kopos_connector.*"],
        exclude=["*.__pycache__", "*.__pycache__.*"],
    ),
    zip_safe=False,
    include_package_data=True,
    package_data={"kopos_connector": ["patches.txt"]},
)

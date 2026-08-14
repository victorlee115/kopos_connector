from setuptools import find_namespace_packages, setup

setup(
    name="kopos_connector",
    # Keep build metadata independent from importing the application package.
    # PEP 517 resolves this file in an isolated environment before the source
    # package is importable; importing kopos_connector here made candidate
    # builds fail before Frappe could install the app.
    version="1.0.11",
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

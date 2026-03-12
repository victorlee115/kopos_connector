from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

with open("README.md") as f:
    long_description = f.read()

setup(
    name="kopos_connector",
    version="1.0.0",
    description="ERPNext connector for KoPOS mobile POS system with modifier and availability management",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="KoPOS",
    author_email="support@kopos.my",
    url="https://github.com/victorlee115/kopos_connector",
    license="GNU GPLv3",
    packages=find_packages(include=["kopos_connector", "kopos_connector.*"]),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Framework :: Frappe",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial :: Point-Of-Sale",
    ],
)

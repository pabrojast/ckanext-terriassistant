from codecs import open
from os import path

from setuptools import find_packages, setup


here = path.abspath(path.dirname(__file__))

with open(path.join(here, "README.rst"), encoding="utf-8") as readme_file:
    long_description = readme_file.read()


setup(
    name="ckanext-terriassistant",
    version="0.1.0",
    description=(
        "CKAN resource view that loads a spatial file into TerriaJS and lets you "
        "style the map through a chat assistant."
    ),
    long_description=long_description,
    url="https://example.invalid/ckanext-terriassistant",
    author="ckanext-terriassistant",
    license="AGPL",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Framework :: CKAN",
    ],
    keywords="CKAN terria map ai chat geospatial",
    packages=find_packages(exclude=["contrib", "docs", "tests*"]),
    include_package_data=True,
    zip_safe=False,
    install_requires=[],
    entry_points="""
        [ckan.plugins]
        terriassistant=ckanext.terriassistant.plugin:TerriassistantPlugin

        [babel.extractors]
        ckan = ckan.lib.extract:extract_ckan
    """,
    message_extractors={
        "ckanext": [
            ("**.py", "python", None),
            ("**.js", "javascript", None),
            ("**/templates/**.html", "ckan", None),
        ],
    },
)

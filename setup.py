try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

setup(
    name='weavehue',
    version='0.8',
    author='Srivatsan Iyer',
    author_email='supersaiyanmode.rox@gmail.com',
    packages=[
        'weavehue',
    ],
    license='MIT',
    description='Philips Hue Plugin for HomeWeave',
    install_requires=[
        'weavelib',
    ]
)

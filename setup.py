# imports -- standard
from setuptools import setup, find_packages

setup(name='respext',
      version='0.1.dev0',
      description='Automatic spectral feature extraction for supernovae',
      url='https://github.com/benstahl92/respext',
      author='Benjamin Stahl',
      author_email='benjamin_stahl@berkeley.edu',
      license='GPL-v3',
      packages=setuptools.find_packages(),
      scripts=['bin/interespext', 'bin/nebularespext'],
      include_package_data=True,
      install_requires=['numpy', 'scipy', 'pandas', 'dill', 'extinction'],
      optional=['matplotlib'],
      classifiers=['Programming Language :: Python',
                   'Programming Language :: Python :: 3',
                   'Topic :: Scientific/Engineering :: Astronomy',
                   'Topic :: Scientific/Engineering :: Physics',
                   'Intended Audience :: Science/Research']
      )

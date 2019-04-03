from setuptools import setup

setup(name='firexk-flame',
      version="0.1",
      description='Core firex libraries',
      url='https://github.com/FireXStuff/firex-flame',
      author='Core FireX Team',
      author_email='firex-dev@gmail.com',
      license='BSD-3-Clause',
      packages=['firex_flame', ],
      zip_safe=True,
      install_requires=["flask"
      ],
      entry_points={
          'console_scripts': ['firex_flame = firex_flame.__main__:main', ]
      },)

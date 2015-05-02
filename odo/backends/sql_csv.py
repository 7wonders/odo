from __future__ import absolute_import, division, print_function

import re
import subprocess
from distutils.spawn import find_executable

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import Executable, ClauseElement

from toolz import merge
from multipledispatch import MDNotImplementedError

from ..append import append
from ..convert import convert
from .csv import CSV, infer_header
from ..temp import Temp
from .aws import S3


class CopyFromCSV(Executable, ClauseElement):
    def __init__(self, element, csv, delimiter=',', header=None, na_value='',
                 lineterminator=r'\n', quotechar='"', escapechar=r'\\',
                 encoding='utf8', skiprows=0, **kwargs):
        if not isinstance(element, sa.Table):
            raise TypeError('element must be a sqlalchemy.Table instance')
        self.element = element
        self.csv = csv
        self.delimiter = delimiter
        self.header = (header if header is not None else
                       (csv.has_header
                        if csv.has_header is not None else infer_header(csv)))
        self.na_value = na_value
        self.lineterminator = lineterminator
        self.quotechar = quotechar
        self.escapechar = escapechar
        self.encoding = encoding
        self.skiprows = skiprows

        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def bind(self):
        return self.element.bind


@compiles(CopyFromCSV, 'sqlite')
def compile_from_csv_sqlite(element, compiler, **kwargs):
    if not find_executable('sqlite3'):
        raise MDNotImplementedError("Could not find sqlite executable")
    cmd = ['sqlite3',
           '-nullvalue', repr(element.na_value),
           '-%sheader' % ('no' if not element.header else ''),
           '-separator', element.delimiter,
           '-cmd', '.import %s %s' % (element.csv.path, element.element.name),
           element.bind.url.database]
    stdout, stderr = subprocess.Popen(cmd,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,
                                      stdin=subprocess.PIPE).communicate()
    assert not stdout and stderr is None, 'bad query %s' % ' '.join(cmd)
    return ''


@compiles(CopyFromCSV, 'mysql')
def compile_from_csv_mysql(element, compiler, **kwargs):
    if element.na_value:
        raise ValueError('MySQL does not support custom NULL values')
    encoding = {'utf-8': 'utf8'}.get(element.encoding.lower(),
                                     element.encoding or 'utf8')
    result = """
        LOAD DATA {local} INFILE '{0.csv.path}'
        INTO TABLE {0.element.name}
        CHARACTER SET {encoding}
        FIELDS
            TERMINATED BY '{0.delimiter}'
            ENCLOSED BY '{0.quotechar}'
            ESCAPED BY '{0.escapechar}'
        LINES TERMINATED BY '{0.lineterminator}'
        IGNORE {0.skiprows} LINES;
    """.format(element,
               local=getattr(element, 'local', ''),
               encoding=encoding).strip()
    return result


@compiles(CopyFromCSV, 'postgresql')
def compile_from_csv_postgres(element, compiler, **kwargs):
    encoding = {'utf8': 'utf-8'}.get(element.encoding.lower(),
                                     element.encoding or 'utf8')
    statement = """
    COPY {0.element.name} FROM '{0.csv.path}'
        (FORMAT CSV,
         DELIMITER E'{0.delimiter}',
         NULL '{0.na_value}',
         QUOTE '{0.quotechar}',
         ESCAPE '{0.escapechar}',
         HEADER {header},
         ENCODING '{encoding}');"""
    return statement.format(element,
                            header=str(element.header).upper(),
                            encoding=encoding).strip()


    return CopyCommand()


try:
    import boto
    from odo.backends.aws import S3
    from redshift_sqlalchemy.dialect import CopyCommand
    import sqlalchemy as sa
except ImportError:
    pass
else:
    @compiles(CopyFromCSV, 'redshift')
    def compile_from_csv_redshift(element, compiler, **kwargs):
        assert isinstance(element.csv, S3(CSV))
        assert element.csv.path.startswith('s3://')

        cfg = boto.Config()

        aws_access_key_id = cfg.get('Credentials', 'aws_access_key_id')
        aws_secret_access_key = cfg.get('Credentials', 'aws_secret_access_key')

        options = dict(delimiter=element.delimiter,
                       ignore_header=int(element.header),
                       empty_as_null=True,
                       blanks_as_null=False,
                       compression=getattr(element, 'compression', ''))

        if getattr(element, 'schema_name', None) is None:
            # 'public' by default, this is a postgres convention
            schema_name = (element.element.schema or
                           sa.inspect(element.bind).default_schema_name)
        cmd = CopyCommand(schema_name=schema_name,
                          table_name=element.element.name,
                          data_location=element.csv.path,
                          access_key=aws_access_key_id,
                          secret_key=aws_secret_access_key,
                          options=options,
                          format='CSV')
        return re.sub(r'\s+(;)', r'\1', re.sub(r'\s+', ' ', str(cmd))).strip()


@append.register(sa.Table, CSV)
def append_csv_to_sql_table(tbl, csv, **kwargs):
    dialect = tbl.bind.dialect.name

    # move things to a temporary S3 bucket if we're using redshift and we
    # aren't already in S3
    if dialect == 'redshift' and not isinstance(csv, S3(CSV)):
        csv = convert(Temp(S3(CSV)), csv, **kwargs)
    elif dialect != 'redshift' and isinstance(csv, S3(CSV)):
        csv = convert(Temp(CSV), csv, has_header=csv.has_header, **kwargs)
    elif dialect == 'hive':
        from .ssh import SSH
        return append(tbl, convert(Temp(SSH(CSV)), csv, **kwargs), **kwargs)

    kwargs = merge(csv.dialect, kwargs)
    stmt = CopyFromCSV(tbl, csv, **kwargs)
    with tbl.bind.begin() as conn:
        conn.execute(stmt)
    return sa.Table(tbl.name,
                    sa.MetaData(bind=tbl.bind),
                    extend_existing=True,
                    autoload=True,
                    schema=tbl.schema)
    return tbl

#!/usr/bin/env python

import sys
import json
import argparse
from collections import OrderedDict
import os

hive_create_template = '''
drop table if exists staging.%(source_name)s_%(schema)s_%(table)s;
create external table staging.%(source_name)s_%(schema)s_%(table)s (
   %(columns_create_newline)s
)            
STORED AS PARQUET
LOCATION '/user/trace/source/%(source_name)s/%(schema)s_%(table)s/CURRENT';
'''

falcon_process_template = '''
<process name="process-INGEST-%(schema)s-%(table)s">
    <tags>type=ingestion source=%(source_name)s schema=%(schema)s ingest_type=%(ingest_type)s</tags>
    <clusters>
        <cluster name="TMDATALAKEP">
            <validity start="2016-01-01T00:00Z" end="2100-01-01T00:00Z"/>
        </cluster>
    </clusters>
    <parallel>1</parallel>
    <order>FIFO</order>
    <frequency>hours(%(frequency_hours)s)</order>
    <timezone>GMT+08:00</timezone>
    <sla shouldStartIn="hours(1)" shouldEndIn="hours(%(should_end_hours)s)"/>
    <validity start="%(start_utc)s" end="2100-01-01T00:00Z" timezone="MYT"/>
    <outputs>
        <output name="output" feed="rawdata-%(schema)s-%(table)s" instance="today(0,0)"/>
    </outputs>
    <workflow engine="oozie" path="%(workflow_path)s"/>
    <properties>
        %(properties)s
    </properties>
</process>
'''


oozie_properties = OrderedDict([
    ('resourceManager','hdpmaster1.tm.com.my:8050'),
    ('jobTracker','hdpmaster1.tm.com.my:8050'),
    ('nameNode','hdfs://hdpmaster1.tm.com.my:8020'),
    ('oozie.wf.application.path','/user/trace/workflows/%(workflow)s/'),
    ('oozie.use.system.libpath','true'),
    ('user.name','trace'),
    ('mapreduce.job.user.name','trace'),
    ('queueName','ingestion'),
    ('prefix', None),
    ('jdbc_uri','jdbc:oracle:thin:@%(host)s:%(port)s/%(tns)s'),
    ('username', None),
    ('password',None),
    ('source_name', None),
    ('schema', None),
    ('table', None),
    ('mapper', None),
    ('split_by', None),
    ('merge_column', None),
    ('check_column', None),
    ('field_delimiter', '~^'),
    ('columns', None),
    ('columns_create', None),
    ('columns_java', None),
    ('columns_flat', None),
])

TYPE_MAP = {
    'VARCHAR2': 'STRING',
    'DATE': 'STRING',
    'NUMBER': 'STRING',
    'CHAR': 'STRING',
    'LONG': 'BIGINT',
    'CLOB': 'BINARY',
    'RAW': 'BINARY'
}

JAVA_TYPE_MAP = {
    'VARCHAR2': 'String',
    'DATE': 'String',
    'NUMBER': 'String',
    'CHAR': 'String',
    'LONG': 'Long',
    'CLOB': 'Bytes',
    'RAW': 'Bytes'
}

STAGES = {
   'dev': {
      'prefix': '/user/trace/development/',
   },
   'prod': {
      'prefix': '/user/trace/'
   }
}

def oozie_config(properties):
    prop = oozie_properties.copy()
    prop['jdbc_uri'] = prop['jdbc_uri'] % properties
    prop['oozie.wf.application.path'] = properties['wfpath']
    for k,v in prop.items():
        if k in properties.keys():
            prop[k] = properties[k]
    return prop

def falcon_process(properties):
    prop = oozie_config(properties)
    params = {
        'schema': properties['schema'],
        'table': properties['table'],
        'ingest_type': properties['workflow'],
        'source_name': properties['source_name'],
        'start_utc': '2017-01-01T00:00Z',
        'should_end_hours': 5,
        'frequency_hours': 24,
        'workflow_path': prop['oozie.wf.application.path'],
        'properties': '\n       '.join(
            ['<property name="%s" value="%s"/>' % (k,v) for (k,v) in prop.items() if '.' not in k])
    }
    job = falcon_process_template % params
    return job

def main():
    argparser = argparse.ArgumentParser(description='Generate oozie and falcon configurations for ingestion')
    argparser.add_argument('profilerjson', help='JSON output from oracle_profiler.py')
    opts = argparser.parse_args()
    hive_create = []
    for ds in json.loads(open(opts.profilerjson).read()):
        for table in ds['tables']:
            mapper = int((table['estimated_size'] or 0) / 1024 / 1024 / 1024) or 2
            if mapper > 20:
                mapper = 20
            columns = [c['field'] for c in table['columns']]
            columns_create = []
            columns_java = []
            columns_flat = []
            for c in table['columns']:
                columns_create.append('`%s` %s' % (c['field'] , TYPE_MAP[c['type']]))
                if JAVA_TYPE_MAP.get(c['type'], None):
                    columns_java.append('%s=%s' % (c['field'], JAVA_TYPE_MAP[c['type']]))

                if TYPE_MAP[c['type']] == 'STRING':
                    columns_flat.append("regexp_replace(`%s`,'\\\\n',' ') AS `%s`" % (c['field'],c['field']))
                else:
                    columns_flat.append("`%s`" % c['field'])

            source_name = ds['datasource']['name'].replace(' ','_')
            username = ds['datasource']['login']
            password = ds['datasource']['password']
   
            if table['merge_key'] and table['check_column']:
                workflow = 'incremental-ingest'
            else:
                workflow = 'full-ingest'
            params = {
                'mapper': mapper, 
                'source_name': source_name,
                'host': ds['datasource']['ip'],
                'port': ds['datasource']['port'],
                'username': username, # ds['datasource']['login'],
                'password': password,
                'tns': ds['datasource']['tns'],
                'schema': ds['datasource']['schema'],
                'table': table['table'],
                'split_by': table['split_by'],
                'columns_java': ','.join(columns_java),
                'columns_create': ','.join(columns_create),
                'columns_create_newline': ',\n    '.join(columns_create),
                'columns_flat': ','.join(columns_flat),
                'columns': ','.join([c['field'] for c in table['columns']]),
                'workflow': workflow,
                'merge_column': table['merge_key'],
                'check_column': table['check_column'],
            }
   
            if params['source_name'] == 'CPC' and params['schema'] == 'SIEBEL' and params['table'] == 'S_CONTACT':
                params['field_delimiter'] = '^~'

            # generate full ingest scripts

            for stage, conf in STAGES.items():
                for ingest in ['full-ingest', 'incremental-ingest', 'incremental-ingest-frozen']:
                    opts = params.copy()
                    opts['wfpath'] = os.path.join(conf['prefix'],'workflows', ingest)
                    opts['prefix'] = conf['prefix']
                    filename = '%(source_name)s-%(schema)s-%(table)s.properties' % opts
                    storedir = 'artifacts/%s-oozie-%s' % (stage, ingest)
                    if 'increment' in ingest and not 'increment' in opts['workflow']:
                       continue
                    if not os.path.exists(storedir):
                        os.makedirs(storedir)
                    with open('%s/%s' % (storedir, filename), 'w') as f:
                        job = '\n'.join(['%s=%s' % (k,v) for k,v in oozie_config(opts).items()])
                        f.write(job)
    
                    filename = '%(source_name)s-%(schema)s-%(table)s.xml' % opts
                    storedir = 'artifacts/%s-falconprocess-%s' % (stage, ingest)
                    if not os.path.exists(storedir): 
                        os.makedirs(storedir)
                    with open('%s/%s' % (storedir,filename), 'w') as f:
                        job = falcon_process(opts)
                        f.write(job)
    
            # generate hive create table
            hive_create.append(hive_create_template % params) 
    
    with open('external-tables.sql', 'w') as f:
         f.write('\n\n'.join(hive_create))

if __name__ == '__main__':
    main()

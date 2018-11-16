import yaml
import glob
from collections import OrderedDict
import pprint

import qiime2.plugins
import importlib
from qiime2.sdk import Result

pp = pprint.PrettyPrinter(indent=2)
pprint = pp.pprint

yaml.add_constructor('!ref', lambda x, y: y)
yaml.add_constructor('!cite', lambda x, y: y)
yaml.add_constructor('!metadata', lambda x, y: y)

def get_import(action, prov_dir, uuid):
    cmd = ['qiime', 'tools', 'import', '--input-path']
    assert len(action['action']['manifest']) == 1
    cmd.append(action['action']['manifest'][0]['name'])
    cmd.append('--input-format')
    cmd.append(action['action']['format'])
    cmd.append('--type')
    with (prov_dir / 'artifacts' / uuid / 'metadata.yaml').open() as fh:
        metadata = yaml.load(fh)
    cmd.append(metadata['type'])
    cmd.append('--output-path')
    cmd.append(str(uuid))
    return ' '.join(cmd)

def get_command(action, prov_dir, uuid):
    if action['action']['type'] == 'import':
        return get_import(action, prov_dir, uuid), []
    cmd = ['qiime']
    cmd.append(action['action']['plugin'].value.split(':')[-1])
    cmd.append(action['action']['action'].replace('_', '-'))
    for param_dict in action['action']['parameters']:
        (param, value), = param_dict.items()
        mod = importlib.import_module(
            'qiime2.plugins.' +
            action['action']['plugin'].value.split(':')[-1].replace('-', '_'),
        )
        print('action!')
        pprint(action['action'])
        parameters = getattr(mod.actions, action['action']['action']).signature.parameters
        print('params')
        pprint(parameters)
        if value != parameters[param].default:
            param_sig = parameters[param]
            if 'Metadata' in param_sig.qiime_type.name:
                cmd.append('--m-' + param.replace('_', '-'))
                cmd.append(str(value))
            elif param_sig.qiime_type.name == 'Bool':
                cmd.append('--p-' + ('' if value else 'no-') + param.replace('_', '-'))
            else:
                cmd.append('--p-' + param.replace('_', '-'))
                cmd.append(str(value))
    required_artifacts = []
    cmd.append('--o-' + action['action']['output-name'].replace('_', '-'))
    cmd.append(str(uuid))
    for imput in action['action']['inputs']:
        (imput, uuids), = imput.items()
        uuids = uuids if type(uuids) == list else [uuids]
        for uuid in uuids:
            if uuid is None:
                continue
            cmd.append('--i-' + imput.replace('_', '-'))
            cmd.append(str(uuid))
            print('whoa', uuid)
            required_artifacts.append(uuid)
    return ' '.join(cmd), required_artifacts

def get_commands(action, prov_dir, uuid=None):
    cmd, dependencies = get_command(action, prov_dir, uuid)
    commands = [cmd]
    print('deps', dependencies)
    for uuid in dependencies:
        print(prov_dir, uuid)
        with (prov_dir / 'artifacts' / uuid / 'action' / 'action.yaml').open() as fh:
            action = yaml.load(fh)
        commands.extend(get_commands(action, prov_dir, uuid))
    return commands


final_artifact = Result.load('/Users/matthew/src/qiime2/paper1/figure1/a-pcoa.qzv')
with (final_artifact._archiver.provenance_dir / 'action' / 'action.yaml').open() as fh:
    action = yaml.load(fh)

commands = reversed(get_commands(action, final_artifact._archiver.provenance_dir, str(final_artifact.uuid)))
for cmd in OrderedDict([(c, None) for c in commands]):
    print(cmd)

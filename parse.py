import yaml
import glob
from collections import OrderedDict

import qiime2.plugins
import importlib
from qiime2.sdk import Result


final_artifact = Result.load('diff.qzv')
results = {}
for result in glob.glob('*.qz*'):
    results[Result.peek(result).uuid] = result


yaml.add_constructor('!ref', lambda x, y: y)
yaml.add_constructor('!cite', lambda x, y: y)

def get_import(action, prov_dir, results, uuid):
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
    cmd.append(results[uuid])
    return ' '.join(cmd)

def get_command(action, results, prov_dir, uuid):
    if action['action']['type'] == 'import':
        return get_import(action, prov_dir, results, uuid), []
    cmd = ['qiime']
    cmd.append(action['action']['plugin'].value.split(':')[-1])
    cmd.append(action['action']['action'].replace('_', '-'))
    for param_dict in action['action']['parameters']:
        (param, value), = param_dict.items()
        mod = importlib.import_module(
            'qiime2.plugins.' + action['action']['plugin'].value.split(':')[-1].replace('-', '_'),
        )
        parameters = getattr(mod.actions, action['action']['action']).signature.parameters
        if value != parameters[param].default:
            param_sig = parameters[param]
            if 'Metadata' in param_sig.qiime_type.name:
                assert False
            elif param_sig.qiime_type.name == 'Bool':
                cmd.append('--p-' + ('' if value else 'no-') + param.replace('_', '-'))
            else:
                cmd.append('--p-' + param.replace('_', '-'))
                cmd.append(str(value))
    required_artifacts = []
    cmd.append('--o-' + action['action']['output-name'].replace('_', '-'))
    cmd.append(results[uuid])
    for imput in action['action']['inputs']:
        (imput, uuid), = imput.items()
        if uuid is None:
            continue
        cmd.append('--i-' + imput.replace('_', '-'))
        cmd.append(results[uuid])
        required_artifacts.append(uuid)
    return ' '.join(cmd), required_artifacts

def get_commands(action, results, prov_dir, uuid=None):
    cmd, dependencies = get_command(action, results, prov_dir, uuid)
    commands = [cmd]
    for uuid in dependencies:
        with (prov_dir / 'artifacts' / uuid / 'action' / 'action.yaml').open() as fh:
            action = yaml.load(fh)
        commands.extend(get_commands(action, results, prov_dir, uuid))
    return commands

with (final_artifact._archiver.provenance_dir / 'action' / 'action.yaml').open() as fh:
    action = yaml.load(fh)

commands = reversed(get_commands(action, results, final_artifact._archiver.provenance_dir, str(final_artifact.uuid)))
for cmd in OrderedDict([(c, None) for c in commands]):
    print(cmd)

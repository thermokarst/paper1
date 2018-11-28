import argparse
import collections
import copy
import pathlib
import pprint

import yaml

from qiime2 import Metadata
from qiime2.metadata.io import MetadataFileError
from qiime2.sdk import Result


yaml.add_constructor('!ref', lambda x, y: y)
yaml.add_constructor('!cite', lambda x, y: y)
yaml.add_constructor('!metadata', lambda x, y: y)


def load_yaml(pathlib_path):
    with pathlib_path.open() as fh:
        return yaml.load(fh)


def kebab(string):
    return string.replace('_', '-')


def dekebab(string):
    return string.replace('-', '_')


def deperiod(string):
    return string.replace('.', '_')


def kebabify_action(command):
    return {
        'plugin': command['plugin'],
        'action': kebab(command['action']),
        'inputs': [(kebab(x), y) for x, y in command['inputs']],
        'metadata': [(kebab(x), y, z) for x, y, z in command['metadata']],
        'parameters': [(kebab(x), y) for x, y in command['parameters']],
        'outputs': [(kebab(x), y) for x, y in command['outputs']],
        'output_dir': command['output_dir'],
    }


# https://stackoverflow.com/a/2158532/313548
def flatten(l):
    for el in l:
        if isinstance(el, list):
            yield from flatten(el)
        else:
            yield el


def is_valid_outdir(parser, outdir):
    outpath = pathlib.Path(outdir)
    if outpath.exists() and outpath.is_dir():
        if list(outpath.iterdir()):
            parser.error('%s is not empty!' % outdir)
    else:
        outpath.mkdir()
    return outpath


def get_import_input_path(command):
    if len(command['action']['manifest']) == 1:
        return command['action']['manifest'][0]['name']
    else:
        return command['action']['format'] + 'import_dir'


def get_output_name(command, uuid, prov_dir):
    if 'output-name' in command['action']:
        return command['action']['output-name'], str(uuid)
    else:  # ooollldddd provenance
        alt_uuid = (prov_dir / 'artifacts' / str(uuid) / 'metadata.yaml')
        mdy = load_yaml(alt_uuid)
        return 'TOO_OLD', mdy['uuid']


def command_is_import(command):
    return command['action']['type'] == 'import'


def param_is_metadata(value):
    return type(value) is yaml.ScalarNode and value.tag == '!metadata'


def load_and_interrogate_metadata(pathlib_md):
    try:
        qmd = Metadata.load(str(pathlib_md))
    except MetadataFileError as e:
        if 'Found unrecognized ID column name' in str(e):
            # This happens when the header row is missing, which is
            # apparently a common thing in pre-2018 provenance.
            md = pathlib_md.parent / 'modified_md.tsv'
            md.write_text(pathlib_md.read_text())
            with md.open('r+') as fh:
                content = fh.read()
                # Gross hack - seed new header row with data from
                # first row.
                first_line = content.split('\n', 1)[0].split('\t')
                first_line[0] = 'id'
                fh.seek(0, 0)
                fh.write('\t'.join(first_line) + '\n' + content)
            qmd = Metadata.load(str(md))
        else:
            raise e

    # Could yield some false positives
    md_type = 'column' if qmd.column_count == 1 else 'full'

    return md_type


def find_metadata_path(filename, prov_dir, uuid):
    if str(uuid) not in str(prov_dir):
        return prov_dir / 'artifacts' / uuid / 'action' / filename
    else:
        return prov_dir / 'action' / filename


def get_import(command_actions, prov_dir, uuid):
    metadata_fp = prov_dir / 'artifacts' / uuid / 'metadata.yaml'
    metadata = load_yaml(metadata_fp)

    return {
        'command_type': 'import',
        'input_path': get_import_input_path(command_actions),
        'input_format': command_actions['action']['format'],
        'type': metadata['type'],
        'output_path': str(uuid),
        'plugins': 'N/A',
        'execution_uuid': command_actions['execution']['uuid'],
    }, []


def get_command(command_actions, prov_dir, output_dir, uuid):
    if command_is_import(command_actions):
        return get_import(command_actions, prov_dir, uuid)

    inputs, metadata, parameters, outputs = [], [], [], []

    for param_dict in command_actions['action']['parameters']:
        # these dicts always have one pair
        (param, value),  = param_dict.items()

        if param_is_metadata(value):
            md = find_metadata_path(value.value, prov_dir, uuid)
            md_type = load_and_interrogate_metadata(md)
            metadata.append((param, '%s.tsv' % uuid, md_type))
        else:
            parameters.append((param, value))

    # TODO: a "pure" provenance solution is unable to determine *all* of the
    # outputs created by an action (unless of course they are somehow all
    # present in the prov graph) --- this means that at present we will need
    # to either interogate the plugin signature (hard to do with old releases),
    # or, manually modify the commands generated. Another option is to use
    # "output_dir" (q2cli) and Results object (Artifact API).
    outputs.append(get_output_name(command_actions, uuid, prov_dir))

    required_dependencies = []
    for input_ in command_actions['action']['inputs']:
        (input_, uuids), = input_.items()
        uuids = uuids if type(uuids) == list else [uuids]
        for uuid in uuids:
            if uuid is None:
                continue
            inputs.append((input_, str(uuid)))
            required_dependencies.append(uuid)

    plugins = command_actions['environment']['plugins']
    plugins = {plugin: plugins[plugin]['version'] for plugin in plugins}

    command = {
        'command_type': 'action',
        'plugin': command_actions['action']['plugin'].value.split(':')[-1],
        'plugins': plugins,
        'action': command_actions['action']['action'],
        'inputs': inputs,
        'metadata': metadata,
        'parameters': parameters,
        'outputs': outputs,
        'execution_uuid': command_actions['execution']['uuid'],
    }

    return command, required_dependencies


def get_commands(final_command_actions, prov_dir, output_dir, uuid=None):
    command, dependencies = get_command(final_command_actions, prov_dir,
                                        output_dir, uuid)
    commands = [command]

    for uuid in dependencies:
        action_yaml = prov_dir / 'artifacts' / uuid / 'action' / 'action.yaml'
        command_action = load_yaml(action_yaml)
        commands.append(
            get_commands(command_action, prov_dir, output_dir, uuid))
    return commands


def parse_provenance(final_command, output_dir):
    final_fp = final_command._archiver.provenance_dir / 'action'
    final_fp = final_fp / 'action.yaml'
    final_command_actions = load_yaml(final_fp)

    commands = get_commands(
        final_command_actions,
        final_command._archiver.provenance_dir,
        output_dir,
        str(final_command.uuid),
    )

    duped_commands = list(reversed(list(flatten(commands))))
    deduped_commands = collections.OrderedDict([])
    for cmd in duped_commands:
        exec_uuid = cmd['execution_uuid']
        if exec_uuid not in deduped_commands:
            deduped_commands[exec_uuid] = cmd
        else:
            if cmd['command_type'] == 'action':
                deduped_commands[exec_uuid]['outputs'].extend(cmd['outputs'])
    return list(deduped_commands.values())


def commands_to_q2cli(final_filename, final_uuid, commands, output_dir):
    results = dict()

    ctr = collections.Counter()

    for command in commands:
        if command['command_type'] == 'action':
            dirname = '%s-%s' % (command['plugin'], command['action'])
            ctr.update([dirname])
            command['output_dir'] = '%s_%d' % (dirname, ctr[dirname])

            for output in command['outputs']:
                if output[1] not in results:
                    ext = '.qzv' if output[0] == 'visualization' else '.qza'
                    results[output[1]] = '%s/%s%s' % (command['output_dir'],
                                                      output[0], ext)
        else:  # import
            results[command['output_path']] = command['input_path'] + '.qza'

    results[final_uuid] = pathlib.Path(final_filename).name

    for command_pos, command in enumerate(commands):
        if command['command_type'] == 'action':
            for input_pos, input_ in enumerate(command['inputs']):
                commands[command_pos]['inputs'][input_pos] = \
                    (input_[0], results[input_[1]])
            for output_pos, output in enumerate(command['outputs']):
                commands[command_pos]['outputs'][output_pos] = \
                    (output[0], results[output[1]])
        else:
            commands[command_pos]['output_path'] = \
                results[command['output_path']]

    outfile = (output_dir / 'q2cli.sh').open('w')
    outfile.write('#!/bin/sh\n\n')

    for command in commands:
        if command['command_type'] == 'action':
            kebab_command = kebabify_action(command)

            cmd = [['qiime', kebab_command['plugin'], kebab_command['action']]]

            for name, value in kebab_command['inputs']:
                cmd.append(['--i-%s' % name, '%s' % value])
            for name, value, md_type in kebab_command['metadata']:
                cmd.append(['--m-%s-file' % name, '%s' % value])
                if md_type == 'column':
                    cmd.append(['--m-%s-column' % name, 'REPLACE_ME'])
            for name, value in kebab_command['parameters']:
                if isinstance(value, bool):
                    cmd.append(['--p-%s%s' % ('' if value else 'no-', name)])
                elif value is not None:
                    cmd.append(['--p-%s' % name, '%s' % value])
            cmd.append(['--output-dir', kebab_command['output_dir']])
        else:
            cmd = [['qiime', 'tools', 'import'],
                   ['--type', "'%s'" % command['type']],
                   ['--input-path', command['input_path']],
                   ['--input-format', command['input_format']],
                   ['--output-path', command['output_path']]]

        cmd = [' '.join(line) for line in cmd]
        comment_line = ['# plugin versions: %s' % command['plugins']]
        first = comment_line + ['%s \\' % cmd[0]]
        last = ['  %s\n' % cmd[-1]]
        cmd = first + ['  %s \\' % line for line in cmd[1:-1]] + last

        outfile.write('%s' % '\n'.join(cmd))
    outfile.close()


def commands_to_artifact_api(final_filename, final_uuid, commands, output_dir):
    results = dict()

    ctr = collections.Counter()

    for command in commands:
        if command['command_type'] == 'action':
            resname = '%s_%s' % (dekebab(command['plugin']),
                                 command['action'])
            ctr.update([resname])
            command['result_name'] = '%s_%d' % (resname, ctr[resname])

            for output in command['outputs']:
                if output[1] not in results:
                    results[output[1]] = '%s.%s' % (command['result_name'],
                                                    output[0])
        else:  # import
            results[command['output_path']] = deperiod(command['input_path'])

    results[final_uuid] = pathlib.Path(final_filename).name

    for command_pos, command in enumerate(commands):
        if command['command_type'] == 'action':
            for input_pos, input_ in enumerate(command['inputs']):
                commands[command_pos]['inputs'][input_pos] = \
                    (input_[0], results[input_[1]])
            for output_pos, output in enumerate(command['outputs']):
                commands[command_pos]['outputs'][output_pos] = \
                    (output[0], results[output[1]])
        else:
            commands[command_pos]['output_path'] = \
                results[command['output_path']]

    outfile = (output_dir / 'artifact_api.sh').open('w')
    outfile.write('#!/usr/bin/env python\n\n')

    for command in commands:
        if command['command_type'] == 'action':
            # TODO: "import" the plugin
            cmd = [['%s = %s.actions.%s(' % (command['result_name'],
                                             dekebab(command['plugin']),
                                             command['action'])]]

            for name, value in command['inputs']:
                cmd.append(['%s=%s,' % (name, value)])
            # TODO: load the metadata
            for name, value, md_type in command['metadata']:
                cmd.append(['# IMPORT METADATA'])
                cmd.append(['%s=%s,' % (name, value)])
            for name, value in command['parameters']:
                cmd.append(['%s=%r,' % (name, value)])
        else:
            # TODO: import view_types
            cmd = [['%s = qiime2.Artifact.import_data(' %
                    command['output_path']],
                   ['%r, %r, view_type=%s' % (command['type'],
                                              command['input_path'],
                                              command['input_format'])]]
            cmd.append(['# IMPORT VIEW TYPE'])

        cmd = [' '.join(line) for line in cmd]
        comment_line = ['# IMPORT PLUGIN\n# plugin versions: %s'
                        % command['plugins']]
        first = comment_line + ['%s' % cmd[0]]
        last = ['  %s\n)\n' % cmd[-1]]
        cmd = first + ['  %s' % line for line in cmd[1:-1]] + last

        outfile.write('%s' % '\n'.join(cmd))
    outfile.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('final_fp', metavar='INPUT_PATH',
                        help='archive to parse')
    parser.add_argument('output_dir', metavar='OUTPUT_PATH',
                        help='directory to output to '
                             '(must be empty/not exist)',
                        type=lambda x: is_valid_outdir(parser, x))

    args = parser.parse_args()

    final_artifact = Result.load(args.final_fp)

    commands = parse_provenance(final_artifact, args.output_dir)

    # TODO: remove this
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(commands)

    commands_to_q2cli(args.final_fp, str(final_artifact.uuid),
                      copy.deepcopy(commands), args.output_dir)
    commands_to_artifact_api(args.final_fp, str(final_artifact.uuid),
                             copy.deepcopy(commands), args.output_dir)

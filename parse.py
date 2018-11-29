import argparse
import collections
import copy
import pathlib

import yaml

import qiime2
from qiime2.metadata.io import MetadataFileError
from qiime2.sdk import Result


yaml.add_constructor('!ref', lambda x, y: y)
yaml.add_constructor('!cite', lambda x, y: y)
yaml.add_constructor('!metadata', lambda x, y: y)


InputRecord = collections.namedtuple('InputRecord', 'name uuid')
MetadataRecord = collections.namedtuple('MetadataRecord', 'name file type')
ParameterRecord = collections.namedtuple('ParameterRecord', 'name value')
OutputRecord = collections.namedtuple('OutputRecord', 'name uuid')
ActionCommand = collections.namedtuple(
    'ActionCommand', 'plugin plugins action inputs metadata '
                     'parameters outputs execution_uuid result'
)
InputCommand = collections.namedtuple(
    'InputCommand', 'input_path input_format type output_path '
                    'plugins execution_uuid'
)

# TODO: add in more of my "learnings" notes
# TODO: check funcs


def load_yaml(pathlib_path):
    with pathlib_path.open() as fh:
        return yaml.load(fh)


def kebab(string):
    return string.replace('_', '-')


def dekebab(string):
    return string.replace('-', '_')


def deperiod(string):
    return string.replace('.', '_')


def kebabify_action(cmd):
    return cmd._replace(
        action=kebab(cmd.action),
        inputs=[(kebab(x), y) for x, y in cmd.inputs],
        metadata=[(kebab(x), y, z) for x, y, z in cmd.metadata],
        parameters=[(kebab(x), y) for x, y in cmd.parameters],
        outputs=[(kebab(x), y) for x, y in cmd.outputs],
    )


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
        return OutputRecord(name=command['action']['output-name'],
                            uuid=str(uuid))
    else:  # ooollldddd provenance
        alt_uuid = (prov_dir / 'artifacts' / str(uuid) / 'metadata.yaml')
        mdy = load_yaml(alt_uuid)
        return OutputRecord(name='TOO_OLD', uuid=mdy['uuid'])


def command_is_import(command):
    return command['action']['type'] == 'import'


def param_is_metadata(value):
    return type(value) is yaml.ScalarNode and value.tag == '!metadata'


def load_and_interrogate_metadata(pathlib_md):
    try:
        qmd = qiime2.Metadata.load(str(pathlib_md))
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
            qmd = qiime2.Metadata.load(str(md))
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

    return InputCommand(
        input_path=get_import_input_path(command_actions),
        input_format=command_actions['action']['format'],
        type=metadata['type'],
        output_path=str(uuid),
        plugins='N/A',
        execution_uuid=command_actions['execution']['uuid'],
    ), []


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
            metadata.append(MetadataRecord(
                name=param, file='%s.tsv' % uuid, type=md_type))
        else:
            parameters.append(ParameterRecord(name=param, value=value))

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
            inputs.append(InputRecord(name=input_, uuid=str(uuid)))
            required_dependencies.append(uuid)

    plugins = command_actions['environment']['plugins']
    plugins = {plugin: plugins[plugin]['version'] for plugin in plugins}

    command = ActionCommand(
        plugin=command_actions['action']['plugin'].value.split(':')[-1],
        plugins=plugins,
        action=command_actions['action']['action'],
        inputs=inputs,
        metadata=metadata,
        parameters=parameters,
        outputs=outputs,
        execution_uuid=command_actions['execution']['uuid'],
        result='',
    )

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
        exec_uuid = cmd.execution_uuid
        if exec_uuid not in deduped_commands:
            deduped_commands[exec_uuid] = cmd
        else:
            if isinstance(cmd, ActionCommand):
                parent_cmd = deduped_commands[exec_uuid]
                deduped_commands[exec_uuid] = \
                    parent_cmd._replace(
                        outputs=parent_cmd.outputs + cmd.outputs)
    return list(deduped_commands.values())


def relabel_command_inputs_and_outputs(final_filename, final_uuid, commands,
                                       q2cli=True):
    results, result_list = dict(), dict()

    ctr = collections.Counter()

    for cmd in commands:
        if isinstance(cmd, ActionCommand):
            resname = '%s-%s' % (cmd.plugin, cmd.action)
            ctr.update([resname])
            result_loc = '%s_%d' % (resname, ctr[resname])
            if not q2cli:
                result_loc = dekebab(result_loc)
            result_list[cmd.execution_uuid] = result_loc

            for output in cmd.outputs:
                if output.uuid not in results:
                    if q2cli:
                        ext = '.qza'
                        if output.name == 'visualization':
                            ext = '.qzv'
                        res = '%s/%s%s' % (result_loc, output.name, ext)
                    else:
                        res = '%s.%s' % (result_list[cmd.execution_uuid],
                                         output.name)
                    results[output.uuid] = res
        else:  # import
            if q2cli:
                res = cmd.input_path + '.qza'
            else:
                res = deperiod(cmd.input_path)
            results[cmd.output_path] = res

    results[final_uuid] = pathlib.Path(final_filename).name

    relabeled_cmds = []
    for cmd in commands:
        if isinstance(cmd, ActionCommand):
            relabeled_inputs = []
            for input_ in cmd.inputs:
                relabeled_inputs.append(input_._replace(
                    uuid=results[input_.uuid]))
            relabeled_outputs = []
            for output in cmd.outputs:
                relabeled_outputs.append(input_._replace(
                    uuid=results[output.uuid]))
            relabeled_cmds.append(cmd._replace(
                inputs=relabeled_inputs, outputs=relabeled_outputs,
                result=result_list[cmd.execution_uuid]))
        else:  # import
            relabeled_cmds.append(cmd._replace(
                output_path=results[cmd.output_path]))
    return relabeled_cmds


def commands_to_q2cli(final_filename, final_uuid, commands, script_dir):
    relabeled_cmds = relabel_command_inputs_and_outputs(final_filename,
                                                        final_uuid,
                                                        commands)

    outfile = (script_dir / 'q2cli.sh').open('w')
    outfile.write('#!/bin/sh\n\n')

    for command in relabeled_cmds:
        if isinstance(command, ActionCommand):
            kcmd = kebabify_action(command)

            cmd = [['qiime', kcmd.plugin, kcmd.action]]

            for name, value in kcmd.inputs:
                cmd.append(['--i-%s' % name, '%s' % value])
            for name, value, md_type in kcmd.metadata:
                cmd.append(['--m-%s-file' % name, '%s' % value])
                if md_type == 'column':
                    cmd.append(['--m-%s-column' % name, 'REPLACE_ME'])
            for name, value in kcmd.parameters:
                if isinstance(value, bool):
                    cmd.append(['--p-%s%s' % ('' if value else 'no-', name)])
                elif value is not None:
                    cmd.append(['--p-%s' % name, '%s' % value])
            cmd.append(['--output-dir', kcmd.result])
        else:
            cmd = [['qiime', 'tools', 'import'],
                   ['--type', "'%s'" % command.type],
                   ['--input-path', command.input_path],
                   ['--input-format', command.input_format],
                   ['--output-path', command.output_path]]

        cmd = [' '.join(line) for line in cmd]
        comment_line = ['# plugin versions: %s' % command.plugins]
        first = comment_line + ['%s \\' % cmd[0]]
        last = ['  %s\n' % cmd[-1]]
        cmd = first + ['  %s \\' % line for line in cmd[1:-1]] + last

        outfile.write('%s' % '\n'.join(cmd))
    outfile.close()


def commands_to_artifact_api(final_filename, final_uuid, commands, output_dir):
    relabeled_cmds = relabel_command_inputs_and_outputs(final_filename,
                                                        final_uuid,
                                                        commands, q2cli=False)

    outfile = (output_dir / 'artifact_api.sh').open('w')
    outfile.write('#!/usr/bin/env python\n\n')

    fmt_cmds, plugins, metadata = [], set(), []
    for command in relabeled_cmds:
        if isinstance(command, ActionCommand):
            plugin = dekebab(command.plugin)

            plugins.add(plugin)

            cmd = [['%s = %s.actions.%s(' % (command.result, plugin,
                                             command.action)]]

            for name, value in command.inputs:
                cmd.append(['%s=%s,' % (name, value)])
            # TODO: load the metadata
            for name, value, md_type in command.metadata:
                cmd.append(['%s=%s,' % (name, value)])
            for name, value in command.parameters:
                cmd.append(['%s=%r,' % (name, value)])

        else:
            # TODO: import view_types
            cmd = [['%s = qiime2.Artifact.import_data(' % command.output_path],
                   ['%r, %r, view_type=%s' % (command.type, command.input_path,
                                              command.input_format)]]
            cmd.append(['# IMPORT VIEW TYPE'])


        cmd = [' '.join(line) for line in cmd]
        comment_line = ['# plugin versions: %s' % command.plugins]
        first = comment_line + ['%s' % cmd[0]]
        last = ['  %s\n)\n' % cmd[-1]]
        fmt_cmds.append(first + ['  %s' % line for line in cmd[1:-1]] + last)

    for plugin in plugins:
        outfile.write('from qiime2.plugins import %s\n' % plugin)

    outfile.write('\n')

    for cmd in fmt_cmds:
        outfile.write('%s' % '\n'.join(cmd))

    # TODO: need to save the resultant viz
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

    commands_to_q2cli(args.final_fp, str(final_artifact.uuid),
                      commands, args.output_dir)
    commands_to_artifact_api(args.final_fp, str(final_artifact.uuid),
                             copy.deepcopy(commands), args.output_dir)

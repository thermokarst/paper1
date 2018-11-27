import argparse
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


def get_import_input_path(node):
    if len(node['action']['manifest']) == 1:
        return node['action']['manifest'][0]['name']
    else:
        return node['action']['format'] + 'import_dir'


def get_output_name(node, uuid, prov_dir):
    if 'output-name' in node['action']:
        return node['action']['output-name'], str(uuid)
    else:  # ooollldddd provenance
        alt_uuid = (prov_dir / 'artifacts' / str(uuid) / 'metadata.yaml')
        mdy = load_yaml(alt_uuid)
        return 'TOO_OLD', mdy['uuid']


def node_is_import(node):
    return node['action']['type'] == 'import'


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
    # TODO: figure out column name

    return md_type


def find_metadata_path(filename, prov_dir, uuid):
    if str(uuid) not in str(prov_dir):
        return prov_dir / 'artifacts' / uuid / 'action' / filename
    else:
        return prov_dir / 'action' / filename


def get_import(node_actions, prov_dir, uuid):
    metadata_fp = prov_dir / 'artifacts' / uuid / 'metadata.yaml'
    metadata = load_yaml(metadata_fp)

    return {
        'node_type': 'import',
        'input_path': get_import_input_path(node_actions),
        'input_format': node_actions['action']['format'],
        'type': metadata['type'],
        'output_path': str(uuid),
    }, []


def get_node(node_actions, prov_dir, output_dir, uuid):
    if node_is_import(node_actions):
        return get_import(node_actions, prov_dir, uuid)

    inputs, metadata, parameters, outputs = [], [], [], []

    for param_dict in node_actions['action']['parameters']:
        # these dicts always have one pair
        (param, value),  = param_dict.items()

        if param_is_metadata(value):
            md = find_metadata_path(value.value, prov_dir, uuid)
            md_type = load_and_interrogate_metadata(md)
            metadata.append((param, '%s.tsv' % uuid, md_type))
        else:
            # TODO: feature-classifier has `null` param vals
            parameters.append((param, value))

    # TODO: a "pure" provenance solution is unable to determine *all* of the
    # outputs created by an action (unless of course they are somehow all
    # present in the prov graph) --- this means that at present we will need
    # to either interogate the plugin signature (hard to do with old releases),
    # or, manually modify the commands generated. Another option is to use
    # "output_dir" (q2cli) and Results object (Artifact API).
    outputs.append(get_output_name(node_actions, uuid, prov_dir))

    required_dependencies = []
    for input_ in node_actions['action']['inputs']:
        (input_, uuids), = input_.items()
        uuids = uuids if type(uuids) == list else [uuids]
        for uuid in uuids:
            if uuid is None:
                continue
            inputs.append((input_, str(uuid)))
            required_dependencies.append(uuid)

    node = {
        'node_type': 'action',
        'plugin': node_actions['action']['plugin'].value.split(':')[-1],
        # TODO: clean up the content of this
        'plugins': node_actions['environment']['plugins'],
        'action': node_actions['action']['action'],
        'inputs': inputs,
        'metadata': metadata,
        'parameters': parameters,
        'outputs': outputs,
    }

    return node, required_dependencies


def get_nodes(final_node_actions, prov_dir, output_dir, uuid=None):
    node, dependencies = get_node(final_node_actions, prov_dir,
                                  output_dir, uuid)
    nodes = [node]

    for uuid in dependencies:
        action_yaml = prov_dir / 'artifacts' / uuid / 'action' / 'action.yaml'
        node_action = load_yaml(action_yaml)
        nodes.append(get_nodes(node_action, prov_dir, output_dir, uuid))
    return nodes


def parse_provenance(final_node, output_dir):
    final_fp = final_node._archiver.provenance_dir / 'action' / 'action.yaml'
    final_node_actions = load_yaml(final_fp)

    nodes = get_nodes(
        final_node_actions,
        final_node._archiver.provenance_dir,
        output_dir,
        str(final_node.uuid),
    )

    nodes = list(flatten(nodes))
    results = dict()

    for node in nodes:
        if node['node_type'] == 'action':
            for output in node['outputs']:
                if output[1] not in results.keys():
                    ext = '.qzv' if output[0] == 'visualization' else '.qza'
                    results[output[1]] = output[0] + ext
        else:
            results[node['output_path']] = node['input_path'] + ext

    for node_pos, node in enumerate(nodes):
        if node['node_type'] == 'action':
            for input_pos, input_ in enumerate(node['inputs']):
                nodes[node_pos]['inputs'][input_pos] = (input_[0],
                                                        results[input_[1]])
            for output_pos, output in enumerate(node['outputs']):
                nodes[node_pos]['outputs'][output_pos] = (output[0],
                                                          results[output[1]])
        else:
            nodes[node_pos]['output_path'] = results[node['output_path']]

    nodes = list(reversed(nodes))

    return nodes


def kebab(string):
    return string.replace('_', '-')


def kebabify_action_node(node):
    return {
        'plugin': node['plugin'],
        'action': kebab(node['action']),
        'inputs': [(kebab(x), y) for x, y in node['inputs']],
        'metadata': [(kebab(x), y) for x, y, _ in node['metadata']],
        'parameters': [(kebab(x), y) for x, y in node['parameters']],
        'outputs': [(kebab(x), y) for x, y in node['outputs']],
    }


def nodes_to_q2cli(nodes, output_dir):
    outfile = (output_dir / 'q2cli.sh').open('w')
    outfile.write('#!/bin/sh\n\n')

    # TODO: do something with plugin deps

    for node in nodes:
        # TODO: Clean this up
        if node['node_type'] == 'action':
            kebab_node = kebabify_action_node(node)

            line = ['qiime']
            line.append(kebab_node['plugin'])
            line.append('%s' % kebab_node['action'])

            cmd = [line]
            for name, value in kebab_node['inputs']:
                cmd.append(['--i-%s' % name, '%s' % value])
            for name, value in kebab_node['metadata']:
                cmd.append(['--m-%s-file' % name, '%s' % value])
            for name, value in kebab_node['parameters']:
                if isinstance(value, bool):
                    cmd.append(['--p-%s%s' % ('' if value else 'no-', name)])
                elif value is not None:
                    cmd.append(['--p-%s' % name, '%s' % value])
            for name, value in kebab_node['outputs']:
                cmd.append(['--o-%s' % name, '%s' % value])
        else:
            cmd = [['qiime', 'tools', 'import'],
                   ['--type', "'%s'" % node['type']],
                   ['--input-path', node['input_path']],
                   ['--input-format', node['input_format']],
                   ['--output-path', node['output_path']]]

        cmd = [' '.join(line) for line in cmd]
        first = ['%s \\' % cmd[0]]
        last = ['  %s\n' % cmd[-1]]
        cmd = first + ['  %s \\' % line for line in cmd[1:-1]] + last

        outfile.write('%s' % '\n'.join(cmd))
    outfile.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('final_artifact', metavar='INPUT_PATH',
                        help='archive to parse', type=Result.load)
    parser.add_argument('output_dir', metavar='OUTPUT_PATH',
                        help='directory to output to '
                             '(must be empty/not exist)',
                        type=lambda x: is_valid_outdir(parser, x))

    args = parser.parse_args()

    nodes = parse_provenance(args.final_artifact, args.output_dir)

    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(nodes)
    # TODO: parse nodes into:
    #   - API format
    nodes_to_q2cli(nodes, args.output_dir)

    # TODO: emit warning about metadata - maybe

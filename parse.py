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


def get_import(node_actions, prov_dir, uuid):
    node = {
        'node_type': 'import',
    }

    if len(node_actions['action']['manifest']) == 1:
        node['input_path'] = node_actions['action']['manifest'][0]['name']
    else:
        node['input_path'] = node_actions['action']['format'] + 'import_dir'
    node['input_format'] = node_actions['action']['format']
    with (prov_dir / 'artifacts' / uuid / 'metadata.yaml').open() as fh:
        metadata = yaml.load(fh)
    node['type'] = metadata['type']
    node['output_path'] = str(uuid)
    return node, []


def get_node(node_actions, prov_dir, output_dir, uuid):
    if node_actions['action']['type'] == 'import':
        return get_import(node_actions, prov_dir, uuid)

    node = {
        'node_type': 'action',
        'plugin': node_actions['action']['plugin'].value.split(':')[-1],
        # TODO: clean this up
        'plugins': node_actions['environment']['plugins'],
        'action': node_actions['action']['action'],
        'inputs': [],
        'metadata': [],
        'parameters': [],
        'outputs': [],
    }

    for param_dict in node_actions['action']['parameters']:
        (param, value), = param_dict.items()

        if type(value) is yaml.ScalarNode and value.tag == '!metadata':
            filename = value.value
            if str(uuid) not in str(prov_dir):
                md = prov_dir / 'artifacts' / str(uuid) / 'action' / filename
            else:
                md = prov_dir / 'action' / filename

            try:
                qmd = Metadata.load(str(md))
            except MetadataFileError as e:
                if 'Found unrecognized ID column name' in str(e):
                    # This happens when the header row is missing, which is
                    # apparently a common thing in pre-2018 provenance.
                    old_md = md
                    md = old_md.parent / 'modified_md.tsv'
                    md.write_text(old_md.read_text())
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

            new_md = '%s.tsv' % uuid
            (output_dir / new_md).write_text(md.read_text())
            node['metadata'].append((param, '%s.tsv' % uuid, md_type))
        else:
            node['parameters'].append((param, value))

    if 'output-name' in node_actions['action']:
        node['outputs'].append((node_actions['action']['output-name'],
                                str(uuid)))
    else:  # ooollldddd provenance
        alt_uuid = (prov_dir / 'artifacts' / str(uuid) / 'metadata.yaml')
        with alt_uuid.open() as fh:
            mdy = yaml.load(fh)
        node['outputs'].append(('TOO_OLD', mdy['uuid']))

    required_dependencies = []
    for input_ in node_actions['action']['inputs']:
        (input_, uuids), = input_.items()
        uuids = uuids if type(uuids) == list else [uuids]
        for uuid in uuids:
            if uuid is None:
                continue
            node['inputs'].append((input_, str(uuid)))
            required_dependencies.append(uuid)
    return node, required_dependencies


def get_nodes(final_node_actions, prov_dir, output_dir, uuid=None):
    node, dependencies = get_node(final_node_actions, prov_dir,
                                  output_dir, uuid)
    nodes = [node]

    for uuid in dependencies:
        action_yaml = prov_dir / 'artifacts' / uuid / 'action' / 'action.yaml'
        with action_yaml.open() as fh:
            node_action = yaml.load(fh)
        nodes.append(get_nodes(node_action, prov_dir, output_dir, uuid))
    return nodes


# https://stackoverflow.com/a/2158532/313548
def flatten(l):
    for el in l:
        if isinstance(el, list):
            yield from flatten(el)
        else:
            yield el


def parse_provenance(final_node, output_dir):
    final_yaml = final_node._archiver.provenance_dir / 'action' / 'action.yaml'
    with final_yaml.open() as fh:
        final_node_actions = yaml.load(fh)

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
            # TODO: rename metadata TSVs
            for metadata_pos, metadata in enumerate(node['metadata']):
                pass
        else:
            nodes[node_pos]['output_path'] = results[node['output_path']]

    nodes = list(reversed(nodes))

    return nodes


def is_valid_outdir(parser, outdir):
    outpath = pathlib.Path(outdir)
    if outpath.exists() and outpath.is_dir():
        if list(outpath.iterdir()):
            parser.error('%s is not empty!' % outdir)
    else:
        outpath.mkdir()
    return outpath


def nodes_to_q2cli(nodes, output_dir):
    outfile = (output_dir / 'q2cli.sh').open('w')
    outfile.write('#!/bin/sh\n\n')

    # TODO: do something with plugin deps

    for node in nodes:
        # TODO: Clean this up
        if node['node_type'] == 'action':
            cmd = ['qiime']
            cmd.append(node['plugin'])
            cmd.append('%s \\\n' % node['action'].replace('_', '-'))
            for name, value in node['inputs']:
                cmd.append(' --i-%s %s \\\n' % (name.replace('_', '-'), value))
            for name, value, _ in node['metadata']:
                cmd.append(' --m-%s-file %s \\\n' %
                           (name.replace('_', '-'), value))
            for name, value in node['parameters']:
                if isinstance(value, bool):
                    cmd.append(' --p-%s%s \\\n' %
                               ('' if value else 'no-',
                                name.replace('_', '-')))
                elif value is not None:
                    cmd.append(' --p-%s %s \\\n' %
                               (name.replace('_', '-'), value))
            for name, value in node['outputs']:
                cmd.append(' --o-%s %s \\\n' % (name.replace('_', '-'), value))
        else:
            cmd = ['qiime', 'tools', 'import', '\\\n',
                   ' --type', "'%s'" % node['type'], '\\\n',
                   ' --input-path', node['input_path'], '\\\n',
                   ' --input-format', node['input_format'], '\\\n',
                   ' --output-path', node['output_path'], '\\\n']

        # Clean up final line
        cmd[-1] = cmd[-1].replace('\\', '')
        outfile.write('%s' % ' '.join(cmd))
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

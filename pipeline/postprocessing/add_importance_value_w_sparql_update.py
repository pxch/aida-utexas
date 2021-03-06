import math
from argparse import ArgumentParser
from operator import itemgetter

from aida_utexas import util
from aida_utexas.aif import JsonGraph, AIDA, LDC, LDC_ONT, UTEXAS

update_prefix = \
    f'PREFIX ldcOnt: {LDC_ONT}\n' \
    f'PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n' \
    f'PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n' \
    f'PREFIX aida: {AIDA}\n' \
    f'PREFIX ldc: {LDC}\n' \
    f'PREFIX utexas: {UTEXAS}\n\n'


def compute_importance_mapping(json_graph, hypothesis, member_to_clusters, cluster_to_prototype):
    stmt_importance = {}
    node_importance = {}

    for stmt_label, stmt_weight in zip(hypothesis['statements'], hypothesis['statementWeights']):
        if stmt_weight <= 0.0:
            stmt_weight = math.exp(stmt_weight / 100.0)
        else:
            stmt_weight = 0.0001

        stmt_subj = json_graph.stmt_subject(stmt_label)
        stmt_pred = json_graph.stmt_predicate(stmt_label)
        stmt_obj = json_graph.stmt_object(stmt_label)

        assert stmt_subj is not None
        assert stmt_pred is not None
        assert stmt_obj is not None

        if stmt_pred != 'type':
            stmt_importance[(stmt_subj, stmt_pred, stmt_obj)] = stmt_weight

        if json_graph.is_event(stmt_subj) or json_graph.is_relation(stmt_subj):
            # if stmt_subj not in node_importance:
            #     node_importance[stmt_subj] = stmt_weight
            # elif node_importance[stmt_subj] < stmt_weight:
            #     node_importance[stmt_subj] = stmt_weight

            for cluster in member_to_clusters[stmt_subj]:
                prototype = cluster_to_prototype.get(cluster, None)
                if prototype is not None:
                    if prototype not in node_importance:
                        node_importance[prototype] = stmt_weight
                    elif node_importance[prototype] < stmt_weight:
                        node_importance[prototype] = stmt_weight

    return stmt_importance, node_importance


def main():
    parser = ArgumentParser()
    parser.add_argument('graph_path', help='path to the graph json file')
    parser.add_argument('hypotheses_path', help='path to the hypotheses json file')
    parser.add_argument('output_dir', help='Directory to write queries')
    parser.add_argument('frame_id', help='Frame ID of the hypotheses')
    parser.add_argument('--top', default=50, type=int,
                        help='number of top hypothesis to output')
    parser.add_argument('-f', '--force', action='store_true', default=False,
                        help='If specified, overwrite existing output files without warning')

    args = parser.parse_args()

    json_graph = JsonGraph.from_dict(util.read_json_file(args.graph_path, 'JSON graph'))
    mappings = json_graph.build_cluster_member_mappings()

    hypotheses_json = util.read_json_file(args.hypotheses_path, 'hypotheses')

    output_dir = util.get_output_dir(args.output_dir, overwrite_warning=not args.force)
    frame_id = args.frame_id

    top_count = 0

    for result_idx, prob in sorted(
            enumerate(hypotheses_json['probs']), key=itemgetter(1), reverse=True):
        if prob <= 0.0:
            hyp_weight = math.exp(prob / 2.0)
        else:
            hyp_weight = 0.0001

        hypothesis = hypotheses_json['support'][result_idx]

        top_count += 1

        hypothesis_id = '{}_hypothesis_{:0>3d}'.format(frame_id, top_count)

        hypothesis_name = 'utexas:{}'.format(hypothesis_id)
        subgraph_name = hypothesis_name + '_subgraph'

        update_query_count = 0

        # Build an update query to add aida:Hypothesis and its importance values, as well as
        # the importance values for all event and relation clusters.
        update_str = update_prefix + 'INSERT DATA\n{\n'
        update_str += '  {} a aida:Hypothesis .\n'.format(hypothesis_name)
        update_str += '  {} aida:importance "{:.4f}"^^xsd:double .\n'.format(
            hypothesis_name, hyp_weight)
        update_str += '  {} aida:hypothesisContent {} .\n'.format(hypothesis_name, subgraph_name)
        update_str += '  {} a aida:Subgraph .\n'.format(subgraph_name)

        stmt_importance, node_importance = compute_importance_mapping(
            json_graph, hypothesis, member_to_clusters=mappings['member_to_clusters'],
            cluster_to_prototype=mappings['cluster_to_prototype'])

        for node_id, importance_value in node_importance.items():
            update_str += '  <{}> aida:importance "{:.4f}"^^xsd:double .\n'.format(
                node_id, importance_value)

        update_str += '}'

        output_path = output_dir / 'hypothesis-{:0>3d}-update-{:0>4d}.rq'.format(
            top_count, update_query_count)

        with open(str(output_path), 'w') as fout:
            fout.write(update_str)

        update_query_count += 1

        # Build an update query for the aida:subgraphContains field of the aida:Subgraph node as
        # the aida:hypothesisContent. We just include all ERE nodes for simplicity, as it's not
        # required that all KEs should be included for NIST to evaluate in M18.
        update_str = update_prefix
        update_str += \
            'INSERT {{\n' \
            '{} aida:subgraphContains ?e .\n' \
            '}}\nWHERE\n{{\n' \
            '{{ ?e a aida:Entity }}\nUNION\n' \
            '{{ ?e a aida:Relation }}\nUNION\n' \
            '{{ ?e a aida:Event }}\n}}\n'.format(subgraph_name)

        output_path = output_dir / 'hypothesis-{:0>3d}-update-{:0>4d}.rq'.format(
            top_count, update_query_count)
        with open(str(output_path), 'w') as fout:
            fout.write(update_str)

        update_query_count += 1

        # Build an update query for the importance value of each statement. We would need
        # a separate query for each statement, because we need to use the INSERT {} WHERE {}
        # operator here to allow BNode statements.
        for (stmt_subj, stmt_pred, stmt_obj), importance_value in stmt_importance.items():
            update_str = update_prefix
            update_str += \
                'INSERT {{ ?x aida:importance "{:.4f}"^^xsd:double . }}\n' \
                'WHERE\n{{\n' \
                '?x a rdf:Statement .\n' \
                '?x rdf:subject <{}> .\n' \
                '?x rdf:predicate ldcOnt:{} .\n' \
                '?x rdf:object <{}> .\n}}\n'.format(
                    importance_value, stmt_subj, stmt_pred, stmt_obj)

            output_path = output_dir / 'hypothesis-{:0>3d}-update-{:0>4d}.rq'.format(
                top_count, update_query_count)

            with open(str(output_path), 'w') as fout:
                fout.write(update_str)

            update_query_count += 1

        if top_count >= args.top:
            break


if __name__ == '__main__':
    main()

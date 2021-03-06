# Original author: Su Wang, 2019
# Modified by Alex Tomkovich in 2019/2020

######
# This file contains the torch modules for GCN coherence network.
######

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from pipeline.training.graph_salads.utils import *

# Scoring function for attention mechanism; either "bilinear" or "concatentative" (both from Luong et al., 2015)
class Score(nn.Module):
    def __init__(self, attention_type, hidden_size):
        super(Score, self).__init__()

        self.attention_type = attention_type

        if attention_type == 'bilinear':
            self.bilinear = nn.Linear(hidden_size, hidden_size)
            torch.nn.init.xavier_uniform_(self.bilinear.weight)

        elif attention_type == 'concat':
            self.concat = nn.Linear(hidden_size * 2, hidden_size)
            torch.nn.init.xavier_uniform_(self.concat.weight)
            self.single = nn.Linear(hidden_size, 1)
            torch.nn.init.xavier_uniform_(self.single.weight)

    # first_inputs is a matrix of attendee embeddings of shape (<num_attendees>, <hidden_dim>)
    # second_inputs is a matrix of attender (candidate) embeddings of shape (<num_attendees>, <hidden_dim>)
    def forward(self, first_inputs, second_inputs):
        if self.attention_type == 'bilinear':
            return self.bilinear(first_inputs).mm(second_inputs.transpose(0, 1))
        elif self.attention_type == 'concat':
            num_attendees = first_inputs.size(0)
            num_attenders = second_inputs.size(0)
            concat_inputs = torch.cat([first_inputs.unsqueeze(1).expand(-1, num_attenders, -1), second_inputs.unsqueeze(0).expand(num_attendees, -1, -1)], dim=-1)
            return self.single(torch.tanh(self.concat(concat_inputs))).squeeze(-1)

# Attention mechanism after Luong et. al, 2015
# Source: https://github.com/tensorflow/nmt#background-on-the-attention-mechanism
class Attention(nn.Module):
    def __init__(self, plaus, attention_type, use_attender_vectors, hidden_size, attention_size, attention_dropout):
        super(Attention, self).__init__()

        if plaus:
            if use_attender_vectors:
                self.linear_plaus = nn.Linear(hidden_size * 2, attention_size)
                torch.nn.init.xavier_uniform_(self.linear_plaus.weight)
            else:
                self.linear_plaus = nn.Linear(hidden_size, attention_size)
                torch.nn.init.xavier_uniform_(self.linear_plaus.weight)
        else:
            self.linear = nn.Linear(hidden_size * 3, attention_size)
            torch.nn.init.xavier_uniform_(self.linear.weight)

        self.plaus = plaus
        self.use_attender_vectors = use_attender_vectors

        if plaus:
            self.score_stmt_to_stmt_plaus = Score(attention_type, hidden_size)
            self.score_ere_to_ere_plaus = Score(attention_type, hidden_size)
        else:
            self.score_stmt_to_stmt = Score(attention_type, hidden_size)
            self.score_ere_to_stmt = Score(attention_type, hidden_size)

        self.att_dropout = torch.nn.Dropout(p=attention_dropout, inplace=False)

    # Compute attention weights (Eq 1 in source)
    # We compute (candidate stmt)-to-(query stmt) and (candidate stmt)-to-(query ERE) weights separately
    def get_attention_weights(self, attendee_stmts, attendee_eres, attender):
        if attendee_stmts is None:
            unnorm_self_attention_weights_eres = self.score_ere_to_ere_plaus(attendee_eres, attender)
            self_attention_weights_eres = F.softmax(unnorm_self_attention_weights_eres, dim=0)
            self_attention_weights_eres = self.att_dropout(self_attention_weights_eres)

            return self_attention_weights_eres
        elif attendee_eres is None:
            unnorm_self_attention_weights_stmts = self.score_stmt_to_stmt_plaus(attendee_stmts, attender)
            self_attention_weights_stmts = F.softmax(unnorm_self_attention_weights_stmts, dim=0)
            self_attention_weights_stmts = self.att_dropout(self_attention_weights_stmts)

            return self_attention_weights_stmts
        else:
            unnorm_attention_weights_stmt_to_stmt = self.score_stmt_to_stmt(attendee_stmts, attender)
            unnorm_attention_weights_ere_to_stmt = self.score_ere_to_stmt(attendee_eres, attender)

            attention_weights_stmt_to_stmt = F.softmax(unnorm_attention_weights_stmt_to_stmt, dim=0)
            attention_weights_ere_to_stmt = F.softmax(unnorm_attention_weights_ere_to_stmt, dim=0)

            attention_weights_stmt_to_stmt = self.att_dropout(attention_weights_stmt_to_stmt)
            attention_weights_ere_to_stmt = self.att_dropout(attention_weights_ere_to_stmt)

            attention_weights = (attention_weights_stmt_to_stmt, attention_weights_ere_to_stmt)

        return attention_weights

    # Calculate context vectors for candidate statements (ala Luong et. al)
    # attendees is a tuple of the form (<stmt_attendees>, <ere_attendees>), where
    # stmt_attendees is the matrix of GCN embeddings for query stmts (and ere_attendees for query EREs)
    # Attention weights is a similar tuple for the two sets of attention weights
    def get_context_vectors(self, attendees, attention_weights):
        if self.plaus:
            context_vectors_self = torch.transpose(attention_weights, 0, 1).mm(attendees)

            return context_vectors_self
        else:
            context_vectors_stmt_to_stmt = torch.transpose(attention_weights[0], 0, 1).mm(attendees[0])
            context_vectors_ere_to_stmt = torch.transpose(attention_weights[1], 0, 1).mm(attendees[1])

            return (context_vectors_stmt_to_stmt, context_vectors_ere_to_stmt)

    # Calculate attention vectors for candidate statements
    def get_attention_vectors(self, attendee_stmts, attendee_eres, attender):
        attention_weights = self.get_attention_weights(attendee_stmts, attendee_eres, attender)

        if self.plaus:
            if attendee_stmts is None:
                context_vectors = self.get_context_vectors(attendee_eres, attention_weights)
            elif attendee_eres is None:
                context_vectors = self.get_context_vectors(attendee_stmts, attention_weights)

            if self.use_attender_vectors:
                attention_vectors = torch.tanh(self.linear_plaus(torch.cat([attender, context_vectors], dim=-1)))
            else:
                attention_vectors = torch.tanh(self.linear_plaus(context_vectors))
        else:
            context_vectors = self.get_context_vectors((attendee_stmts, attendee_eres), attention_weights)

            # Concat candidate statements with their context vectors and feed into a tanh layer to produce attention vectors
            attention_vectors = torch.tanh(self.linear(torch.cat([attender, context_vectors[0], context_vectors[1]], dim=-1)))

        return attention_vectors

# Simple conversion to tensor
def to_tensor(inputs, tensor_type=torch.LongTensor, device=torch.device("cpu")):
    return tensor_type(np.array(inputs)).to(device)

# Class defining the GCN architecture
# forward() method runs graphs through GCN and attention mechanism
class CoherenceNetWithGCN(nn.Module):
    def __init__(self, plaus, indexer_info_dict, attention_type, use_attender_vectors, num_layers, hidden_size, attention_size, conv_dropout, attention_dropout):
        super(CoherenceNetWithGCN, self).__init__()
        ere_emb = indexer_info_dict['ere_emb_mat']
        stmt_emb = indexer_info_dict['stmt_emb_mat']
        ere_embed_size = ere_emb.shape[1]
        stmt_embed_size = stmt_emb.shape[1]
        self.ere_embedder = nn.Embedding.from_pretrained(torch.from_numpy(ere_emb), freeze=False)
        self.stmt_embedder = nn.Embedding.from_pretrained(torch.from_numpy(stmt_emb), freeze=False)

        self.linear_head_adj_stmt_init = nn.Linear(stmt_embed_size, hidden_size)
        self.linear_tail_adj_stmt_init = nn.Linear(stmt_embed_size, hidden_size)
        self.linear_type_adj_stmt_init = nn.Linear(stmt_embed_size, hidden_size)
        self.linear_head_adj_ere_init = nn.Linear(hidden_size, hidden_size)
        self.linear_tail_adj_ere_init = nn.Linear(hidden_size, hidden_size)
        self.linear_head_adj_stmt = nn.Linear(hidden_size, hidden_size)
        self.linear_tail_adj_stmt = nn.Linear(hidden_size, hidden_size)
        self.linear_type_adj_stmt = nn.Linear(hidden_size, hidden_size)
        self.linear_head_adj_ere = nn.Linear(hidden_size, hidden_size)
        self.linear_tail_adj_ere = nn.Linear(hidden_size, hidden_size)
        self.linear_ere_init = nn.Linear(ere_embed_size, hidden_size)
        self.linear_stmt_init = nn.Linear(stmt_embed_size, hidden_size)
        self.linear_ere = nn.Linear(hidden_size, hidden_size)
        self.linear_stmt = nn.Linear(hidden_size, hidden_size)

        self.plaus = plaus

        self.coherence_attention = Attention(plaus, attention_type, use_attender_vectors, hidden_size, attention_size, attention_dropout)
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.conv_dropout = torch.nn.Dropout(p=conv_dropout, inplace=False)
        self.attention_type = attention_type

        if plaus:
            self.linear_plaus = nn.Linear((attention_size * 2), 1)
            torch.nn.init.xavier_uniform_(self.linear_plaus.weight)
        else:
            self.coherence_linear = nn.Linear(attention_size, 1)
            torch.nn.init.xavier_uniform_(self.coherence_linear.weight)

        # Only needed if num_layers >= 3
        if self.num_layers >= 3:
            self.linear_head_adj_stmt_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_tail_adj_stmt_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_type_adj_stmt_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_head_adj_ere_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_tail_adj_ere_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_ere_3 = nn.Linear(hidden_size, hidden_size)
            self.linear_stmt_3 = nn.Linear(hidden_size, hidden_size)

            torch.nn.init.xavier_uniform_(self.linear_head_adj_stmt_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_tail_adj_stmt_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_type_adj_stmt_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_head_adj_ere_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_tail_adj_ere_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_ere_3.weight)
            torch.nn.init.xavier_uniform_(self.linear_stmt_3.weight)

        # Only needed if num_layers == 4
        if self.num_layers == 4:
            self.linear_head_adj_stmt_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_tail_adj_stmt_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_type_adj_stmt_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_head_adj_ere_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_tail_adj_ere_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_ere_4 = nn.Linear(hidden_size, hidden_size)
            self.linear_stmt_4 = nn.Linear(hidden_size, hidden_size)

            torch.nn.init.xavier_uniform_(self.linear_head_adj_stmt_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_tail_adj_stmt_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_type_adj_stmt_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_head_adj_ere_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_tail_adj_ere_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_ere_4.weight)
            torch.nn.init.xavier_uniform_(self.linear_stmt_4.weight)

        torch.nn.init.xavier_uniform_(self.linear_head_adj_stmt_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_tail_adj_stmt_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_type_adj_stmt_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_head_adj_ere_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_tail_adj_ere_init.weight)

        torch.nn.init.xavier_uniform_(self.linear_head_adj_stmt.weight)
        torch.nn.init.xavier_uniform_(self.linear_tail_adj_stmt.weight)
        torch.nn.init.xavier_uniform_(self.linear_type_adj_stmt.weight)
        torch.nn.init.xavier_uniform_(self.linear_head_adj_ere.weight)
        torch.nn.init.xavier_uniform_(self.linear_tail_adj_ere.weight)

        torch.nn.init.xavier_uniform_(self.linear_ere_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_stmt_init.weight)
        torch.nn.init.xavier_uniform_(self.linear_ere.weight)
        torch.nn.init.xavier_uniform_(self.linear_stmt.weight)

    # Runs a graph salad through the GCN network
    def gcn(self, graph_dict, gcn_embeds, device):
        adj_head = torch.from_numpy(graph_dict['adj_head']).to(dtype=torch.float, device=device)
        adj_tail = torch.from_numpy(graph_dict['adj_tail']).to(dtype=torch.float, device=device)
        adj_type = torch.from_numpy(graph_dict['adj_type']).to(dtype=torch.float, device=device)
        ere_labels = graph_dict['ere_labels']
        stmt_labels = graph_dict['stmt_labels']

        ere_emb = torch.zeros((adj_head.shape[0], self.ere_embedder.weight.shape[1])).to(device=device)
        stmt_emb = torch.zeros((adj_head.shape[1], self.stmt_embedder.weight.shape[1])).to(device=device)

        # Fetch and average embeddings for ERE/stmt names and labels
        for iter in range(len(ere_labels)):
            ere_emb[iter] = torch.mean(torch.cat([self.ere_embedder(to_tensor(label_set, device=device)).mean(dim=0).reshape((1, -1)) for label_set in ere_labels[iter] if len(label_set) > 0], dim=0), dim=0)

        for iter in range(len(stmt_labels)):
            stmt_emb[iter] = torch.mean(torch.cat([self.stmt_embedder(to_tensor(label_set, device=device)).mean(dim=0).reshape((1, -1)) for label_set in stmt_labels[iter] if len(label_set) > 0], dim=0), dim=0)

        # Layer 1
        gcn_embeds['eres'] = self.linear_ere_init(ere_emb)
        gcn_embeds['eres'] += torch.mm(adj_head, self.linear_head_adj_stmt_init(stmt_emb))
        gcn_embeds['eres'] += torch.mm(adj_tail, self.linear_tail_adj_stmt_init(stmt_emb))
        gcn_embeds['eres'] += torch.mm(adj_type, self.linear_type_adj_stmt_init(stmt_emb))
        gcn_embeds['eres'] = self.conv_dropout(F.relu(gcn_embeds['eres']))

        gcn_embeds['stmts'] = self.linear_stmt_init(stmt_emb)
        gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_head, 0, 1), self.linear_head_adj_ere_init(gcn_embeds['eres']))
        gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_tail, 0, 1), self.linear_tail_adj_ere_init(gcn_embeds['eres']))
        gcn_embeds['stmts'] = self.conv_dropout(F.relu(gcn_embeds['stmts']))

        # Layer 2
        gcn_embeds['eres'] = self.linear_ere(gcn_embeds['eres'])
        gcn_embeds['eres'] += torch.mm(adj_head, self.linear_head_adj_stmt(gcn_embeds['stmts']))
        gcn_embeds['eres'] += torch.mm(adj_tail, self.linear_tail_adj_stmt(gcn_embeds['stmts']))
        gcn_embeds['eres'] += torch.mm(adj_type, self.linear_type_adj_stmt(gcn_embeds['stmts']))
        gcn_embeds['eres'] = self.conv_dropout(F.relu(gcn_embeds['eres']))

        gcn_embeds['stmts'] = self.linear_stmt(gcn_embeds['stmts'])
        gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_head, 0, 1), self.linear_head_adj_ere(gcn_embeds['eres']))
        gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_tail, 0, 1), self.linear_tail_adj_ere(gcn_embeds['eres']))
        gcn_embeds['stmts'] = self.conv_dropout(F.relu(gcn_embeds['stmts']))

        # Layer 3 (if applicable)
        if self.num_layers >= 3:
            gcn_embeds['eres'] = self.linear_ere_3(gcn_embeds['eres'])
            gcn_embeds['eres'] += torch.mm(adj_head, self.linear_head_adj_stmt_3(gcn_embeds['stmts']))
            gcn_embeds['eres'] += torch.mm(adj_tail, self.linear_tail_adj_stmt_3(gcn_embeds['stmts']))
            gcn_embeds['eres'] += torch.mm(adj_type, self.linear_type_adj_stmt_3(gcn_embeds['stmts']))
            gcn_embeds['eres'] = self.conv_dropout(F.relu(gcn_embeds['eres']))

            gcn_embeds['stmts'] = self.linear_stmt_3(gcn_embeds['stmts'])
            gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_head, 0, 1), self.linear_head_adj_ere_3(gcn_embeds['eres']))
            gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_tail, 0, 1), self.linear_tail_adj_ere_3(gcn_embeds['eres']))
            gcn_embeds['stmts'] = self.conv_dropout(F.relu(gcn_embeds['stmts']))

        # Layer 4 (if applicable)
        if self.num_layers == 4:
            gcn_embeds['eres'] = self.linear_ere_4(gcn_embeds['eres'])
            gcn_embeds['eres'] += torch.mm(adj_head, self.linear_head_adj_stmt_4(gcn_embeds['stmts']))
            gcn_embeds['eres'] += torch.mm(adj_tail, self.linear_tail_adj_stmt_4(gcn_embeds['stmts']))
            gcn_embeds['eres'] += torch.mm(adj_type, self.linear_type_adj_stmt_4(gcn_embeds['stmts']))
            gcn_embeds['eres'] = self.conv_dropout(F.relu(gcn_embeds['eres']))

            gcn_embeds['stmts'] = self.linear_stmt_4(gcn_embeds['stmts'])
            gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_head, 0, 1), self.linear_head_adj_ere_4(gcn_embeds['eres']))
            gcn_embeds['stmts'] += torch.mm(torch.transpose(adj_tail, 0, 1), self.linear_tail_adj_ere_4(gcn_embeds['eres']))
            gcn_embeds['stmts'] = self.conv_dropout(F.relu(gcn_embeds['stmts']))

    def forward(self, graph_dict, gcn_embeds, device):
        # Only calculate GCN embeds for the first in a series of extractions; otherwise, keep passing/retaining the initial embeds
        if not gcn_embeds:
            gcn_embeds = {'eres': dict(), 'stmts': dict()}
            self.gcn(graph_dict, gcn_embeds, device)

        stmt_attendees = gcn_embeds['stmts'][graph_dict['query_stmts']]
        ere_attendees = gcn_embeds['eres'][list(graph_dict['query_eres'])]

        if self.plaus:
            self_att_vectors_stmts = self.coherence_attention.get_attention_vectors(stmt_attendees, None, stmt_attendees)
            self_att_vectors_eres = self.coherence_attention.get_attention_vectors(None, ere_attendees, ere_attendees)

            stmts_vector = torch.mean(self_att_vectors_stmts, dim=0)
            eres_vector = torch.mean(self_att_vectors_eres, dim=0)

            final_vector = torch.cat([stmts_vector, eres_vector], dim=0)

            plaus_out = self.linear_plaus(final_vector)

            return plaus_out, gcn_embeds
        else:
            attenders = gcn_embeds['stmts'][graph_dict['candidates']]

            # Get attention vectors for candidate statements
            coherence_attention_vectors = self.coherence_attention.get_attention_vectors(stmt_attendees, ere_attendees, attenders)

            # Obtain a final set of logits for the candidate statements (softmax later)
            coherence_out = self.coherence_linear(coherence_attention_vectors).squeeze(-1)

            return coherence_attention_vectors, coherence_out, gcn_embeds
from typing import List, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GCNConv
from torch_geometric.nn.models import GAE
from torch_geometric.data import Data


class TimeEncoder(nn.Module):
    """
    TimeEncode class of GraphMIXER method https://github.com/CongWeilin/GraphMixer/blob/main/model.py#L32.
    """

    def __init__(self, dim):
        super(TimeEncoder, self).__init__()
        self.dim = dim
        self.w = nn.Linear(1, dim)
        self.reset_parameters()

    def reset_parameters(self, ):
        self.w.weight = nn.Parameter(
            (torch.from_numpy(1 / 10 ** np.linspace(0, 9, self.dim, dtype=np.float32))).reshape(self.dim, -1))
        self.w.bias = nn.Parameter(torch.zeros(self.dim))

        self.w.weight.requires_grad = False
        self.w.bias.requires_grad = False

    @torch.no_grad()
    def forward(self, t):
        t = t.float()
        output = torch.cos(self.w(t.reshape((-1, 1)))).squeeze()
        return output


class MPNN(nn.Module):
    def __init__(
            self,
            input_dim: int,
            hidden_dim: int,
            output_dim: int
    ) -> None:
        super(MPNN, self).__init__()
        self.mp1 = GCNConv(in_channels=input_dim, out_channels=hidden_dim)
        self.mp2 = GCNConv(in_channels=hidden_dim, out_channels=hidden_dim)
        self.mp3 = GCNConv(in_channels=hidden_dim, out_channels=output_dim)

        self.bn1 = nn.BatchNorm1d(num_features=hidden_dim)
        self.bn2 = nn.BatchNorm1d(num_features=hidden_dim)
        self.bn3 = nn.BatchNorm1d(num_features=hidden_dim)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, normalize: bool = False) -> torch.Tensor:
        z = self.mp1(x, edge_index)
        z = self.bn1(z)
        z = F.relu(z)
        z = F.dropout(z, p=0.1, training=self.training)

        z = self.mp2(z, edge_index)
        z = self.bn2(z)
        z = F.relu(z)
        z = F.dropout(z, p=0.1, training=self.training)

        z = self.mp3(z, edge_index)
        z = self.bn3(z)
        if normalize:
            z = F.normalize(z, p=2., dim=-1)
        z = F.dropout(z, p=0.1, training=self.training)
        return z


class GGRU(nn.Module):
    def __init__(
            self,
            struct_embed_dim: int,
            state_dim: int
    ):
        super(GGRU, self).__init__()
        self.state_dim = state_dim

        self.Wi_reset = GCNConv(in_channels=struct_embed_dim, out_channels=state_dim, improved=True)
        self.Ws_reset = GCNConv(in_channels=state_dim, out_channels=state_dim, improved=True)

        self.Wi_update = GCNConv(in_channels=struct_embed_dim, out_channels=state_dim, improved=True)
        self.Ws_update = GCNConv(in_channels=state_dim, out_channels=state_dim, improved=True)

        self.Wi_cand = GCNConv(in_channels=struct_embed_dim, out_channels=state_dim, improved=True)
        self.Ws_cand = GCNConv(in_channels=state_dim, out_channels=state_dim, improved=True)

    def forward(
            self,
            z: torch.Tensor,
            edge_index: torch.Tensor,
            s: torch.Tensor,
            edge_weight: torch.Tensor = None,
    ) -> torch.Tensor:
        reset_gate = torch.sigmoid(
            self.Wi_reset(z, edge_index, edge_weight) + self.Ws_reset(s, edge_index, edge_weight))
        update_gate = torch.sigmoid(
            self.Wi_update(z, edge_index, edge_weight) + self.Ws_update(s, edge_index, edge_weight))
        s_candidate = torch.tanh(
            self.Wi_cand(z, edge_index, edge_weight) + reset_gate * self.Ws_cand(s, edge_index, edge_weight))
        s = (1 - update_gate) * s_candidate + update_gate * s
        return s


class TENENCE(nn.Module):
    def __init__(
            self,
            input_dim: int,
            hidden_dim: int,
            output_dim: int
    ) -> None:
        super(TENENCE, self).__init__()
        self.output_dim = output_dim
        self.gae = GAE(encoder=MPNN(input_dim=input_dim, hidden_dim=hidden_dim, output_dim=output_dim))
        self.update = GGRU(struct_embed_dim=2 * output_dim, state_dim=output_dim)
        self.timestep_enc = TimeEncoder(dim=output_dim)
        self.decoder = nn.Linear(in_features=output_dim, out_features=output_dim)
        self.link_predictor = nn.Linear(in_features=output_dim, out_features=output_dim)
        self.predictive_encoder_local = nn.Sequential(
            nn.Linear(in_features=2 * output_dim, out_features=2 * output_dim),
            nn.ReLU(),
            nn.Linear(in_features=2 * output_dim, out_features=output_dim),
        )
        self.predictive_encoder_global = nn.Linear(in_features=2 * output_dim, out_features=output_dim)

    def forward(self, snapshot_sequence: List[Data], normalize: bool = False) -> torch.Tensor:
        # encoder
        states, state, Z_enc, Z_dec, Z_pred = self.encode_sequence(snapshot_sequence, normalize)

        # computing loss
        reconstruction_loss, infoNCE, prediction_loss = self.compute_losses(snapshot_sequence,
                                                                            states, Z_enc, Z_dec, Z_pred)

        loss = reconstruction_loss + infoNCE + prediction_loss
        return loss

    def encode_sequence(
            self,
            snapshot_sequence: List[Data],
            normalize: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        num_nodes = snapshot_sequence[0].x.size(0)
        Z_enc = []
        Z_dec = []
        Z_pred = []

        state = torch.zeros(num_nodes, self.output_dim)
        last_seen = torch.zeros(num_nodes, dtype=torch.float)
        states = []
        for k, graph in enumerate(snapshot_sequence):
            # snapshot data
            x_k = graph.x.to_dense()
            edge_index_k = graph.edge_index
            node_mask_k = graph.node_mask

            # gae encoder call
            z_enc_k = self.gae.encode(x_k, edge_index_k, normalize=normalize)
            Z_enc.append(z_enc_k.unsqueeze(0))

            # updating last seen embedding for state update
            src = edge_index_k[0, :].unique()
            last_seen = last_seen.index_fill(0, src, k + 1)
            last_seen_enc_k = self.timestep_enc(last_seen)

            # state update
            z_enc_k = torch.cat([z_enc_k, last_seen_enc_k], dim=1)
            state = self.update(z_enc_k, edge_index_k, state)
            states.append(state.unsqueeze(0))

            # GAE decoder
            z_dec_k = self.dec(state)
            Z_dec.append(z_dec_k.unsqueeze(0))

            # prediction decoder
            z_pred_k = self.pred(state)
            Z_pred.append(z_pred_k.unsqueeze(0))
        states = torch.cat(states, dim=0)
        Z_enc = torch.cat(Z_enc, dim=0)
        Z_dec = torch.cat(Z_dec, dim=0)
        Z_pred = torch.cat(Z_pred, dim=0)
        return states, state, Z_enc, Z_dec, Z_pred

    def decode_next(
            self,
            snapshot_sequence: List[Data],
            normalize: bool = False
    ):
        states, state, Z_enc, Z_dec, Z_pred = self.encode(snapshot_sequence, normalize)
        z_pred = Z_pred[-1]
        probs = self.gae.decoder.forward_all(z_pred, sigmoid=True)
        return probs

    def compute_losses(self, snapshot_sequence: List[Data], states, Z_enc, Z_dec, Z_pred):
        num_timesteps = len(snapshot_sequence)
        num_nodes = snapshot_sequence[0].x.size(0)
        reconstruction_loss = torch.tensor(0.0)
        infoNCE = torch.tensor(0.0)
        prediction_loss = torch.tensor(0.0)
        ks = torch.arange(len(snapshot_sequence)).unsqueeze(0) + 1
        ks_enc = self.k_enc(ks)
        # losses
        for k, graph in enumerate(snapshot_sequence):
            # reconstruction loss at k
            z_dec_k = Z_dec[k]
            edge_index_k = snapshot_sequence[k].edge_index
            recon_loss_k = self.gae.recon_loss(z_dec_k, edge_index_k)
            reconstruction_loss += recon_loss_k

            # prediction loss at k
            if k < num_timesteps - 1:
                z_pred_k = Z_pred[k]
                edge_index_next = snapshot_sequence[k + 1].edge_index
                pred_loss_k = self.gae.recon_loss(z_pred_k, edge_index_next)
                prediction_loss += pred_loss_k

                # infoNCE loss at k
                state_k = states[k]
                ks_enc_future_expanded = ks_enc[k + 1:].unsqueeze(1).repeat(1, num_nodes, 1)
                state_k_expanded = state_k.unsqueeze(0).repeat(len(ks_enc_future_expanded), 1, 1)
                summary_state_k_expanded = state_k.mean(0).unsqueeze(0).repeat(len(ks_enc[k + 1:]), 1)
                z_cpc_local_future = self.cpc_local(torch.cat([state_k_expanded, ks_enc_future_expanded], dim=-1))
                z_cpc_global_future = self.cpc_global(torch.cat([summary_state_k_expanded, ks_enc[k+1:]], dim=-1))
                z_local_future = Z_enc[k + 1:]
                z_global_future = Z_enc[k + 1:].mean(1)

                # positive scores
                # local
                scores_same_k = torch.einsum("TND, LMD -> TNM", z_cpc_local_future, z_local_future)
                pos_scores_k_local = torch.diagonal(scores_same_k, dim1=1, dim2=2)

                # global
                pos_scores_k_global = torch.diagonal(z_cpc_global_future @ z_global_future.T)

                # negative scores
                # local
                # neg_scores_same_k_different_node
                same_k_not_same_nodes_mask = ~torch.eye(num_nodes, dtype=torch.bool).unsqueeze(0).repeat(
                    len(scores_same_k), 1, 1)
                neg_scores_same_k_different_node = scores_same_k[same_k_not_same_nodes_mask]

                # neg_scores_not_same_k_all_nodes
                not_same_k_mask = ~torch.eye(num_timesteps, dtype=torch.bool)[k + 1:]
                neg_scores_not_same_k_all_nodes = []
                for idx in range(ks[:, k + 1:].size(1)):
                    neg_score_kp1 = torch.einsum("ND, TMD -> TNM", z_cpc_local_future[idx], Z_enc[not_same_k_mask[idx]])
                    neg_scores_not_same_k_all_nodes.append(neg_score_kp1.unsqueeze(0))
                neg_scores_not_same_k_all_nodes = torch.cat(neg_scores_not_same_k_all_nodes, dim=0)

                # global
                # neg_scores_k_global: not_same_k
                neg_scores_k_global = (z_cpc_global_future @ Z_enc.mean(1).T)[not_same_k_mask]

                # infoNCE loss computation
                pos_scores_k = torch.cat([pos_scores_k_local.flatten(), pos_scores_k_global.flatten()], dim=0)
                neg_scores_k = torch.cat([neg_scores_same_k_different_node.flatten(),
                                          neg_scores_not_same_k_all_nodes.flatten(),
                                          neg_scores_k_global.flatten()], dim=0)
                pos_labels = torch.ones_like(pos_scores_k)
                neg_labels = torch.zeros_like(neg_scores_k)
                infoNCE_k = F.binary_cross_entropy_with_logits(
                    input=torch.cat([pos_scores_k, neg_scores_k], dim=0),
                    target=torch.cat([pos_labels, neg_labels], dim=0),
                    pos_weight=torch.tensor(len(neg_labels) / len(pos_labels))
                )
                infoNCE += infoNCE_k
        return reconstruction_loss, infoNCE, prediction_loss

    @staticmethod
    def compute_local_infoNCE_loss(Z_hat: torch.Tensor, Z: torch.Tensor):
        num_timesteps = Z.size(0)

        # exclude z_hat_0 and z_0 since z_hat_0 == z_0
        pos_scores = []
        neg_scores = []
        for k in range(num_timesteps):
            z_hat_k = Z_hat[k]
            z_k = Z[k]
            scores_all = torch.einsum("ND, TMD -> TNM", z_hat_k, Z)

            pos_scores_k = torch.diagonal(scores_all[k])
            pos_scores.append(pos_scores_k)

            # spatial negatives
            # any time-different nodes
            non_diagonal_mask = ~torch.eye(z_k.size(0), dtype=torch.bool)
            spatial_mask = non_diagonal_mask.unsqueeze(0).repeat(num_timesteps, 1, 1)
            spatial_neg_scores_k = scores_all[spatial_mask]
            neg_scores.append(spatial_neg_scores_k)

            # temporal negatives
            # same node - different time
            negative_times = torch.tensor(list(set(range(num_timesteps)).difference({k})), dtype=torch.long)
            scores_all_except_k = scores_all[negative_times, :, :]
            temporal_mask = ~spatial_mask[:-1]
            temporal_neg_scores_k = scores_all_except_k[temporal_mask]
            neg_scores.append(temporal_neg_scores_k)
        pos_scores = torch.cat(pos_scores)
        neg_scores = torch.cat(neg_scores)
        scores = torch.cat([pos_scores, neg_scores])
        pos_labels = torch.ones_like(pos_scores, dtype=torch.float)
        neg_labels = torch.zeros_like(neg_scores, dtype=torch.float)
        labels = torch.cat([pos_labels, neg_labels])
        infoNCE = F.binary_cross_entropy_with_logits(
            input=scores,
            target=labels,
            pos_weight=torch.tensor(len(neg_labels) / len(pos_labels))
        )
        return infoNCE

    @staticmethod
    def compute_global_infoNCE_loss(Z_hat: torch.Tensor, Z: torch.Tensor, compute_f1: bool):
        Z = Z.mean(1)
        scores_all = Z_hat @ Z.T

        pos_scores = torch.diagonal(scores_all)
        temporal_mask = ~torch.eye(Z.size(0), dtype=torch.bool)
        neg_scores = scores_all[temporal_mask]

        scores = torch.cat([pos_scores, neg_scores])
        pos_labels = torch.ones_like(pos_scores, dtype=torch.float)
        neg_labels = torch.zeros_like(neg_scores, dtype=torch.float)
        labels = torch.cat([pos_labels, neg_labels])
        infoNCE = F.binary_cross_entropy_with_logits(
            input=scores,
            target=labels,
            pos_weight=torch.tensor(len(neg_labels) / len(pos_labels))
        )

        return infoNCE
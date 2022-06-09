from functools import partial
from warnings import warn

import numpy as np
import torch
from nnsvs.base import BaseModel, PredictionType
from nnsvs.dsp import TrTimeInvFIRFilter
from nnsvs.layers.layer_norm import LayerNorm
from nnsvs.mdn import MDNLayer, mdn_get_most_probable_sigma_and_mu
from nnsvs.multistream import split_streams
from nnsvs.util import init_weights
from torch import nn
from torch.nn.utils import weight_norm
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


def WNConv1d(*args, **kwargs):
    return weight_norm(nn.Conv1d(*args, **kwargs))


class ResnetBlock(nn.Module):
    def __init__(self, dim, dilation=1):
        super().__init__()
        self.block = nn.Sequential(
            nn.LeakyReLU(0.2),
            nn.ReflectionPad1d(dilation),
            WNConv1d(dim, dim, kernel_size=3, dilation=dilation),
            nn.LeakyReLU(0.2),
            WNConv1d(dim, dim, kernel_size=1),
        )
        self.shortcut = WNConv1d(dim, dim, kernel_size=1)

    def forward(self, x):
        return self.shortcut(x) + self.block(x)


class Conv1dResnet(BaseModel):
    """Conv1d + Resnet

    The model is inspired by the MelGAN's model architecture (:cite:t:`kumar2019melgan`).
    MDN layer is added if use_mdn is True.

    Args:
        in_dim (int): the dimension of the input
        hidden_dim (int): the dimension of the hidden state
        out_dim (int): the dimension of the output
        num_layers (int): the number of layers
        init_type (str): the type of weight initialization
        use_mdn (bool): whether to use MDN or not
        num_gaussians (int): the number of gaussians in MDN
        dim_wise (bool): whether to use dim-wise or not
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=4,
        init_type="none",
        use_mdn=False,
        num_gaussians=8,
        dim_wise=False,
        **kwargs,
    ):
        super().__init__()
        self.use_mdn = use_mdn

        if "dropout" in kwargs:
            warn(
                "dropout argument in Conv1dResnet is deprecated"
                " and will be removed in future versions"
            )

        model = [
            nn.ReflectionPad1d(3),
            WNConv1d(in_dim, hidden_dim, kernel_size=7, padding=0),
        ]
        for n in range(num_layers):
            model.append(ResnetBlock(hidden_dim, dilation=2 ** n))

        last_conv_out_dim = hidden_dim if use_mdn else out_dim
        model += [
            nn.LeakyReLU(0.2),
            nn.ReflectionPad1d(3),
            WNConv1d(hidden_dim, last_conv_out_dim, kernel_size=7, padding=0),
        ]

        self.model = nn.Sequential(*model)

        if self.use_mdn:
            self.mdn_layer = MDNLayer(
                in_dim=hidden_dim,
                out_dim=out_dim,
                num_gaussians=num_gaussians,
                dim_wise=dim_wise,
            )
        else:
            self.mdn_layer = None

        init_weights(self, init_type)

    def prediction_type(self):
        return (
            PredictionType.PROBABILISTIC
            if self.use_mdn
            else PredictionType.DETERMINISTIC
        )

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        out = self.model(x.transpose(1, 2)).transpose(1, 2)

        if self.use_mdn:
            return self.mdn_layer(out)
        else:
            return out

    def inference(self, x, lengths=None):
        """Inference step

        Find the most likely mean and variance if use_mdn is True

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor

        Returns:
            tuple: mean and variance of the output features
        """
        if self.use_mdn:
            log_pi, log_sigma, mu = self(x, lengths)
            sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
            return mu, sigma
        else:
            return self(x, lengths)


@torch.no_grad()
def _shallow_ar_inference(out, stream_sizes, analysis_filts):
    from torchaudio.functional import lfilter

    out_streams = split_streams(out, stream_sizes)
    # back to conv1d friendly (B, C, T) format
    out_streams = map(lambda x: x.transpose(1, 2), out_streams)

    out_syn = []
    for sidx, os in enumerate(out_streams):
        out_stream_syn = torch.zeros_like(os)
        a = analysis_filts[sidx].get_filt_coefs()
        # apply IIR filter for each dimiesion
        for idx in range(os.shape[1]):
            # NOTE: scipy.signal.lfilter accespts b, a in order,
            # but torchaudio expect the oppsite; a, b in order
            ai = a[idx].view(-1).flip(0)
            bi = torch.zeros_like(ai)
            bi[0] = 1
            out_stream_syn[:, idx, :] = lfilter(os[:, idx, :], ai, bi, clamp=False)
        out_syn += [out_stream_syn]

    out_syn = torch.cat(out_syn, 1)
    return out_syn.transpose(1, 2)


class Conv1dResnetSAR(Conv1dResnet):
    """Conv1dResnet with shallow AR structure

    Args:
        stream_sizes (list): Stream sizes
        ar_orders (list): Filter dimensions for each stream.
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=4,
        stream_sizes=None,
        ar_orders=None,
        init_type="none",
        **kwargs,
    ):
        super().__init__(
            in_dim=in_dim, hidden_dim=hidden_dim, out_dim=out_dim, num_layers=num_layers
        )

        if "dropout" in kwargs:
            warn(
                "dropout argument in Conv1dResnetSAR is deprecated"
                " and will be removed in future versions"
            )

        if stream_sizes is None:
            stream_sizes = [180, 3, 1, 15]
        if ar_orders is None:
            ar_orders = [20, 200, 20, 20]
        self.stream_sizes = stream_sizes

        init_weights(self, init_type)

        self.analysis_filts = nn.ModuleList()
        for s, K in zip(stream_sizes, ar_orders):
            self.analysis_filts += [TrTimeInvFIRFilter(s, K + 1)]

    def preprocess_target(self, y):
        assert sum(self.stream_sizes) == y.shape[-1]
        ys = split_streams(y, self.stream_sizes)
        for idx, yi in enumerate(ys):
            ys[idx] = self.analysis_filts[idx](yi.transpose(1, 2)).transpose(1, 2)
        return torch.cat(ys, -1)

    def inference(self, x, lengths=None, y=None):
        out = self.model(x.transpose(1, 2)).transpose(1, 2)
        return _shallow_ar_inference(out, self.stream_sizes, self.analysis_filts)


class FFN(BaseModel):
    """Feed-forward network

    Args:
        in_dim (int): the dimension of the input
        hidden_dim (int): the dimension of the hidden state
        out_dim (int): the dimension of the output
        num_layers (int): the number of layers
        dropout (float): dropout rate
        init_type (str): the type of weight initialization
        last_sigmoid (bool): whether to apply sigmoid on the output
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=2,
        dropout=0.0,
        init_type="none",
        last_sigmoid=False,
    ):
        super(FFN, self).__init__()
        self.first_linear = nn.Linear(in_dim, hidden_dim)
        self.hidden_layers = nn.ModuleList(
            [nn.Linear(hidden_dim, hidden_dim) for _ in range(num_layers)]
        )
        self.last_linear = nn.Linear(hidden_dim, out_dim)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.last_sigmoid = last_sigmoid
        init_weights(self, init_type)

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        h = self.relu(self.first_linear(x))
        for hl in self.hidden_layers:
            h = self.dropout(self.relu(hl(h)))
        out = self.last_linear(h)
        out = torch.sigmoid(out) if self.last_sigmoid else out
        return out


# For compatibility
FeedForwardNet = FFN


class LSTMRNN(BaseModel):
    """LSTM-based recurrent neural network

    Args:
        in_dim (int): the dimension of the input
        hidden_dim (int): the dimension of the hidden state
        out_dim (int): the dimension of the output
        num_layers (int): the number of layers
        bidirectional (bool): whether to use bidirectional LSTM
        dropout (float): dropout rate
        init_type (str): the type of weight initialization
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=1,
        bidirectional=True,
        dropout=0.0,
        init_type="none",
    ):
        super(LSTMRNN, self).__init__()
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.num_direction = 2 if bidirectional else 1
        self.lstm = nn.LSTM(
            in_dim,
            hidden_dim,
            num_layers,
            bidirectional=bidirectional,
            batch_first=True,
            dropout=dropout,
        )
        self.hidden2out = nn.Linear(self.num_direction * self.hidden_dim, out_dim)
        init_weights(self, init_type)

    def forward(self, x, lengths, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        if isinstance(lengths, torch.Tensor):
            lengths = lengths.to("cpu")
        x = pack_padded_sequence(x, lengths, batch_first=True)
        out, _ = self.lstm(x)
        out, _ = pad_packed_sequence(out, batch_first=True)
        out = self.hidden2out(out)
        return out


class LSTMRNNSAR(LSTMRNN):
    """LSTM-RNN with shallow AR structure

    Args:
        stream_sizes (list): Stream sizes
        ar_orders (list): Filter dimensions for each stream.
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=1,
        bidirectional=True,
        dropout=0.0,
        stream_sizes=None,
        ar_orders=None,
        init_type="none",
    ):
        super().__init__(
            in_dim, hidden_dim, out_dim, num_layers, bidirectional, dropout, init_type
        )
        if stream_sizes is None:
            stream_sizes = [180, 3, 1, 15]
        if ar_orders is None:
            ar_orders = [20, 200, 20, 20]

        self.stream_sizes = stream_sizes
        self.analysis_filts = nn.ModuleList()
        for s, K in zip(stream_sizes, ar_orders):
            self.analysis_filts += [TrTimeInvFIRFilter(s, K + 1)]

    def preprocess_target(self, y):
        assert sum(self.stream_sizes) == y.shape[-1]
        ys = split_streams(y, self.stream_sizes)
        for idx, yi in enumerate(ys):
            ys[idx] = self.analysis_filts[idx](yi.transpose(1, 2)).transpose(1, 2)
        return torch.cat(ys, -1)

    def inference(self, x, lengths=None, y=None):
        out = self.forward(x, lengths)
        return _shallow_ar_inference(out, self.stream_sizes, self.analysis_filts)


class RMDN(BaseModel):
    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=1,
        bidirectional=True,
        dropout=0.0,
        num_gaussians=8,
        dim_wise=False,
        init_type="none",
    ):
        """RNN-based mixture density networks (MDN)

        Args:
            in_dim (int): the dimension of the input
            hidden_dim (int): the dimension of the hidden state
            out_dim (int): the dimension of the output
            num_layers (int): the number of layers
            bidirectional (bool): whether to use bidirectional LSTM
            dropout (float): dropout rate
            num_gaussians (int): the number of gaussians
            dim_wise (bool): whether to use dimension-wise or not
            init_type (str): the type of weight initialization
        """
        super(RMDN, self).__init__()
        self.linear = nn.Linear(in_dim, hidden_dim)
        self.relu = nn.ReLU()
        self.num_direction = 2 if bidirectional else 1
        self.lstm = nn.LSTM(
            hidden_dim,
            hidden_dim,
            num_layers,
            bidirectional=bidirectional,
            batch_first=True,
            dropout=dropout,
        )
        self.mdn = MDNLayer(
            in_dim=self.num_direction * hidden_dim,
            out_dim=out_dim,
            num_gaussians=num_gaussians,
            dim_wise=dim_wise,
        )
        init_weights(self, init_type)

    def prediction_type(self):
        return PredictionType.PROBABILISTIC

    def forward(self, x, lengths, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        if isinstance(lengths, torch.Tensor):
            lengths = lengths.to("cpu")
        out = self.linear(x)
        sequence = pack_padded_sequence(self.relu(out), lengths, batch_first=True)
        out, _ = self.lstm(sequence)
        out, _ = pad_packed_sequence(out, batch_first=True)
        out = self.mdn(out)
        return out

    def inference(self, x, lengths=None):
        """Inference step

        Find the most likely mean and variance

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor

        Returns:
            tuple: mean and variance of the output features
        """
        log_pi, log_sigma, mu = self.forward(x, lengths)
        sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
        return mu, sigma


class MDN(BaseModel):
    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=1,
        num_gaussians=8,
        dim_wise=False,
        init_type="none",
        **kwargs,
    ):
        """Mixture density networks (MDN) with FFN

        Args:
            in_dim (int): the dimension of the input
            hidden_dim (int): the dimension of the hidden state
            out_dim (int): the dimension of the output
            num_layers (int): the number of layers
            num_gaussians (int): the number of gaussians
            dim_wise (bool): whether to use dimension-wise or not
            init_type (str): the type of weight initialization
        """
        super(MDN, self).__init__()
        if "dropout" in kwargs:
            warn(
                "dropout argument in MDN is deprecated"
                " and will be removed in future versions"
            )
        model = [nn.Linear(in_dim, hidden_dim), nn.ReLU()]
        if num_layers > 1:
            for _ in range(num_layers - 1):
                model += [nn.Linear(hidden_dim, hidden_dim), nn.ReLU()]
        model += [
            MDNLayer(
                in_dim=hidden_dim,
                out_dim=out_dim,
                num_gaussians=num_gaussians,
                dim_wise=dim_wise,
            )
        ]
        self.model = nn.Sequential(*model)
        init_weights(self, init_type)

    def prediction_type(self):
        return PredictionType.PROBABILISTIC

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        return self.model(x)

    def inference(self, x, lengths=None):
        """Inference step

        Find the most likely mean and variance

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor

        Returns:
            tuple: mean and variance of the output features
        """
        log_pi, log_sigma, mu = self.forward(x, lengths)
        sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
        return mu, sigma


# Will be removed in future versions
class Conv1dResnetMDN(BaseModel):
    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=4,
        num_gaussians=8,
        dim_wise=False,
        init_type="none",
        **kwargs,
    ):
        super().__init__()

        if "dropout" in kwargs:
            warn(
                "dropout argument in Conv1dResnet is deprecated"
                " and will be removed in future versions"
            )

        model = [
            Conv1dResnet(
                in_dim=in_dim,
                hidden_dim=hidden_dim,
                out_dim=hidden_dim,
                num_layers=num_layers,
            ),
            nn.ReLU(),
            MDNLayer(
                in_dim=hidden_dim,
                out_dim=out_dim,
                num_gaussians=num_gaussians,
                dim_wise=dim_wise,
            ),
        ]
        self.model = nn.Sequential(*model)
        init_weights(self, init_type)

    def prediction_type(self):
        return PredictionType.PROBABILISTIC

    def forward(self, x, lengths=None, y=None):
        return self.model(x)

    def inference(self, x, lengths=None):
        log_pi, log_sigma, mu = self.forward(x, lengths)
        sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
        return mu, sigma


def predict_lf0_with_residual(
    in_feats,
    out_feats,
    in_lf0_idx=300,
    in_lf0_min=5.3936276,
    in_lf0_max=6.491111,
    out_lf0_idx=180,
    out_lf0_mean=5.953093881972361,
    out_lf0_scale=0.23435173188961034,
    residual_f0_max_cent=600,
):
    """Predict log-F0 with residual.

    Args:
        in_feats (np.ndarray): input features
        out_feats (np.ndarray): output of an acoustic model
        in_lf0_idx (int): index of LF0 in input features
        in_lf0_min (float): minimum value of LF0 in the training data of input features
        in_lf0_max (float): maximum value of LF0 in the training data of input features
        out_lf0_idx (int): index of LF0 in output features
        out_lf0_mean (float): mean of LF0 in the training data of output features
        out_lf0_scale (float): scale of LF0 in the training data of output features
        residual_f0_max_cent (int): maximum value of residual LF0 in cent

    Returns:
        tuple: (predicted log-F0, residual log-F0)
    """
    # Denormalize lf0 from input musical score
    lf0_score = in_feats[:, :, in_lf0_idx].unsqueeze(-1)
    lf0_score_denorm = lf0_score * (in_lf0_max - in_lf0_min) + in_lf0_min

    # To avoid unbounded residual f0 that would potentially cause artifacts,
    # let's constrain the residual F0 to be in a certain range by the scaled tanh
    max_lf0_ratio = residual_f0_max_cent * np.log(2) / 1200

    if len(out_feats.shape) == 4:
        # MDN case (B, T, num_gaussians, C) -> (B, T, num_gaussians)
        lf0_residual = out_feats[:, :, :, out_lf0_idx]
    else:
        # (B, T, C) -> (B, T, 1)
        lf0_residual = out_feats[:, :, out_lf0_idx].unsqueeze(-1)
    lf0_residual = max_lf0_ratio * torch.tanh(lf0_residual)

    # Residual connection in the denormalized f0 domain
    lf0_pred_denorm = lf0_score_denorm + lf0_residual

    # Back to normalized f0
    lf0_pred = (lf0_pred_denorm - out_lf0_mean) / out_lf0_scale

    return lf0_pred, lf0_residual


class ResF0Conv1dResnet(BaseModel):
    """Conv1d + Resnet + Residual F0 prediction

    Residual F0 prediction is inspired by  :cite:t:`hono2021sinsy`.

    Args:
        in_dim (int): input dimension
        hidden_dim (int): hidden dimension
        out_dim (int): output dimension
        num_layers (int): number of layers
        in_lf0_idx (int): index of lf0 in input features
        in_lf0_min (float): minimum value of lf0 in the training data of input features
        in_lf0_max (float): maximum value of lf0 in the training data of input features
        out_lf0_idx (int): index of lf0 in output features. Typically 180.
        out_lf0_mean (float): mean of lf0 in the training data of output features
        out_lf0_scale (float): scale of lf0 in the training data of output features
        init_type (str): initialization type
        use_mdn (bool): whether to use MDN or not
        num_gaussians (int): number of gaussians in MDN
        dim_wise (bool): whether to use dimension-wise MDN or not
    """

    def __init__(
        self,
        in_dim,
        hidden_dim,
        out_dim,
        num_layers=4,
        # NOTE: you must carefully set the following parameters
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
        init_type="none",
        use_mdn=False,
        num_gaussians=8,
        dim_wise=False,
    ):
        super().__init__()
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale
        self.use_mdn = use_mdn

        model = [
            nn.ReflectionPad1d(3),
            WNConv1d(in_dim, hidden_dim, kernel_size=7, padding=0),
        ]
        for n in range(num_layers):
            model.append(ResnetBlock(hidden_dim, dilation=2 ** n))

        last_conv_out_dim = hidden_dim if use_mdn else out_dim
        model += [
            nn.LeakyReLU(0.2),
            nn.ReflectionPad1d(3),
            WNConv1d(hidden_dim, last_conv_out_dim, kernel_size=7, padding=0),
        ]
        self.model = nn.Sequential(*model)

        if self.use_mdn:
            self.mdn_layer = MDNLayer(
                in_dim=hidden_dim,
                out_dim=out_dim,
                num_gaussians=num_gaussians,
                dim_wise=dim_wise,
            )
        else:
            self.mdn_layer = None

        init_weights(self, init_type)

    def prediction_type(self):
        return (
            PredictionType.PROBABILISTIC
            if self.use_mdn
            else PredictionType.DETERMINISTIC
        )

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): input features
            lengths (torch.Tensor): lengths of input features
            y (torch.Tensor): output features

        Returns:
            tuple: (output features, residual log-F0)
        """
        out = self.model(x.transpose(1, 2)).transpose(1, 2)

        if self.use_mdn:
            log_pi, log_sigma, mu = self.mdn_layer(out)
        else:
            mu = out

        lf0_pred, lf0_residual = predict_lf0_with_residual(
            x,
            mu,
            self.in_lf0_idx,
            self.in_lf0_min,
            self.in_lf0_max,
            self.out_lf0_idx,
            self.out_lf0_mean,
            self.out_lf0_scale,
        )

        # Inject the predicted lf0 into the output features
        if self.use_mdn:
            mu[:, :, :, self.out_lf0_idx] = lf0_pred.squeeze(-1)
        else:
            mu[:, :, self.out_lf0_idx] = lf0_pred.squeeze(-1)

        if self.use_mdn:
            return (log_pi, log_sigma, mu), lf0_residual
        else:
            return mu, lf0_residual

    def inference(self, x, lengths=None):
        """Inference step

        Args:
            x (torch.Tensor): input features
            lengths (torch.Tensor): lengths of input features

        Returns:
            tuple: (mu, sigma) if use_mdn, (output, ) otherwise
        """
        if self.use_mdn:
            (log_pi, log_sigma, mu), _ = self(x, lengths)
            sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
            return mu, sigma
        else:
            return self(x, lengths)[0]


# For backward compatibility
ResF0Conv1dResnetMDN = partial(ResF0Conv1dResnet, use_mdn=True)


class ResSkipF0FFConvLSTM(BaseModel):
    """FFN + Conv1d + LSTM + residual/skip connections

    A model proposed in :cite:t:`hono2021sinsy`.

    Args:
        in_dim (int): input dimension
        ff_hidden_dim (int): hidden dimension of feed-forward layer
        conv_hidden_dim (int): hidden dimension of convolutional layer
        lstm_hidden_dim (int): hidden dimension of LSTM layer
        out_dim (int): output dimension
        dropout (float): dropout rate
        num_ls (int): number of layers of LSTM
        bidirectional (bool): whether to use bidirectional LSTM or not
        in_lf0_idx (int): index of lf0 in input features
        in_lf0_min (float): minimum of lf0 in the training data of input features
        in_lf0_max (float): maximum of lf0 in the training data of input features
        out_lf0_idx (int): index of lf0 in output features
        out_lf0_mean (float): mean of lf0 in the training data of output features
        out_lf0_scale (float): scale of lf0 in the training data of output features
        skip_inputs (bool): whether to use skip connection for the input features
        init_type (str): initialization type
        use_mdn (bool): whether to use MDN or not
        num_gaussians (int): number of gaussians in MDN
        dim_wise (bool): whether to use MDN with dim-wise or not
    """

    def __init__(
        self,
        in_dim,
        ff_hidden_dim=2048,
        conv_hidden_dim=1024,
        lstm_hidden_dim=256,
        out_dim=199,
        dropout=0.0,
        num_lstm_layers=2,
        bidirectional=True,
        # NOTE: you must carefully set the following parameters
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
        skip_inputs=False,
        init_type="none",
        use_mdn=False,
        num_gaussians=8,
        dim_wise=False,
    ):
        super().__init__()
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale
        self.skip_inputs = skip_inputs
        self.use_mdn = use_mdn

        self.ff = nn.Sequential(
            nn.Linear(in_dim, ff_hidden_dim),
            nn.ReLU(),
            nn.Linear(ff_hidden_dim, ff_hidden_dim),
            nn.ReLU(),
            nn.Linear(ff_hidden_dim, ff_hidden_dim),
            nn.ReLU(),
        )

        self.conv = nn.Sequential(
            nn.ReflectionPad1d(3),
            nn.Conv1d(ff_hidden_dim + 1, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
            nn.ReflectionPad1d(3),
            nn.Conv1d(conv_hidden_dim, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
            nn.ReflectionPad1d(3),
            nn.Conv1d(conv_hidden_dim, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
        )

        num_direction = 2 if bidirectional else 1
        self.lstm = nn.LSTM(
            conv_hidden_dim,
            lstm_hidden_dim,
            num_lstm_layers,
            bidirectional=True,
            batch_first=True,
            dropout=dropout,
        )

        if self.skip_inputs:
            last_in_dim = num_direction * lstm_hidden_dim + in_dim
        else:
            last_in_dim = num_direction * lstm_hidden_dim

        if self.use_mdn:
            self.mdn_layer = MDNLayer(
                last_in_dim, out_dim, num_gaussians=num_gaussians, dim_wise=dim_wise
            )
        else:
            self.fc = nn.Linear(last_in_dim, out_dim)

        init_weights(self, init_type)

    def prediction_type(self):
        return (
            PredictionType.PROBABILISTIC
            if self.use_mdn
            else PredictionType.DETERMINISTIC
        )

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        if isinstance(lengths, torch.Tensor):
            lengths = lengths.to("cpu")

        lf0_score = x[:, :, self.in_lf0_idx].unsqueeze(-1)

        out = self.ff(x)
        out = torch.cat([out, lf0_score], dim=-1)

        out = self.conv(out.transpose(1, 2)).transpose(1, 2)
        sequence = pack_padded_sequence(out, lengths, batch_first=True)
        out, _ = self.lstm(sequence)
        out, _ = pad_packed_sequence(out, batch_first=True)
        out = torch.cat([out, x], dim=-1) if self.skip_inputs else out

        if self.use_mdn:
            log_pi, log_sigma, mu = self.mdn_layer(out)
        else:
            mu = self.fc(out)

        lf0_pred, lf0_residual = predict_lf0_with_residual(
            x,
            mu,
            self.in_lf0_idx,
            self.in_lf0_min,
            self.in_lf0_max,
            self.out_lf0_idx,
            self.out_lf0_mean,
            self.out_lf0_scale,
        )

        # Inject the predicted lf0 into the output features
        if self.use_mdn:
            mu[:, :, :, self.out_lf0_idx] = lf0_pred.squeeze(-1)
        else:
            mu[:, :, self.out_lf0_idx] = lf0_pred.squeeze(-1)

        if self.use_mdn:
            return (log_pi, log_sigma, mu), lf0_residual
        else:
            return mu, lf0_residual

    def inference(self, x, lengths=None):
        """Inference step

        Args:
            x (torch.Tensor): input features
            lengths (torch.Tensor): lengths of input features

        Returns:
            tuple: (mu, sigma) if use_mdn, (output, ) otherwise
        """
        if self.use_mdn:
            (log_pi, log_sigma, mu), _ = self(x, lengths)
            sigma, mu = mdn_get_most_probable_sigma_and_mu(log_pi, log_sigma, mu)
            return mu, sigma
        else:
            return self(x, lengths)[0]


class FFConvLSTM(BaseModel):
    def __init__(
        self,
        in_dim,
        ff_hidden_dim=2048,
        conv_hidden_dim=1024,
        lstm_hidden_dim=256,
        out_dim=199,
        dropout=0.0,
        num_lstm_layers=2,
        bidirectional=True,
        init_type="none",
    ):
        """FFN + Conv1d + LSTM

        A model proposed in :cite:t:`hono2021sinsy` without residual F0 prediction.
        """
        super().__init__()

        self.ff = nn.Sequential(
            nn.Linear(in_dim, ff_hidden_dim),
            nn.ReLU(),
            nn.Linear(ff_hidden_dim, ff_hidden_dim),
            nn.ReLU(),
            nn.Linear(ff_hidden_dim, ff_hidden_dim),
            nn.ReLU(),
        )

        self.conv = nn.Sequential(
            nn.ReflectionPad1d(3),
            nn.Conv1d(ff_hidden_dim, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
            nn.ReflectionPad1d(3),
            nn.Conv1d(conv_hidden_dim, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
            nn.ReflectionPad1d(3),
            nn.Conv1d(conv_hidden_dim, conv_hidden_dim, kernel_size=7, padding=0),
            nn.BatchNorm1d(conv_hidden_dim),
            nn.ReLU(),
        )

        num_direction = 2 if bidirectional else 1
        self.lstm = nn.LSTM(
            conv_hidden_dim,
            lstm_hidden_dim,
            num_lstm_layers,
            bidirectional=True,
            batch_first=True,
            dropout=dropout,
        )

        last_in_dim = num_direction * lstm_hidden_dim
        self.fc = nn.Linear(last_in_dim, out_dim)
        init_weights(self, init_type)

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        if isinstance(lengths, torch.Tensor):
            lengths = lengths.to("cpu")

        out = self.ff(x)
        out = self.conv(out.transpose(1, 2)).transpose(1, 2)
        sequence = pack_padded_sequence(out, lengths, batch_first=True)
        out, _ = self.lstm(sequence)
        out, _ = pad_packed_sequence(out, batch_first=True)
        out = self.fc(out)

        return out


class VariancePredictor(BaseModel):
    """Variance predictor in :cite:t:`ren2020fastspeech`.

    The model is basically composed of stacks of Conv1d + ReLU + BatchNorm1d layers.

    Args:
        in_dim (int): the input dimension
        out_dim (int): the output dimension
        num_layers (int): the number of layers
        hidden_dim (int): the hidden dimension
        kernel_size (int): the kernel size
        dropout (float): the dropout rate
    """

    def __init__(
        self, in_dim, out_dim, num_layers=5, hidden_dim=256, kernel_size=5, dropout=0.5
    ):
        super().__init__()
        conv = nn.ModuleList()
        for idx in range(num_layers):
            in_channels = in_dim if idx == 0 else hidden_dim
            conv += [
                nn.Sequential(
                    nn.Conv1d(
                        in_channels,
                        hidden_dim,
                        kernel_size,
                        stride=1,
                        padding=(kernel_size - 1) // 2,
                    ),
                    nn.ReLU(),
                    LayerNorm(hidden_dim, dim=1),
                    nn.Dropout(dropout),
                )
            ]
        self.conv = nn.Sequential(*conv)
        self.fc = nn.Linear(hidden_dim, out_dim)

    def forward(self, x, lengths=None, y=None):
        """Forward step

        Args:
            x (torch.Tensor): the input tensor
            lengths (torch.Tensor): the lengths of the input tensor
            y (torch.Tensor): the optional target tensor

        Returns:
            torch.Tensor: the output tensor
        """
        x = self.conv(x.transpose(1, 2))
        return self.fc(x.transpose(1, 2))


class ResF0VariancePredictor(VariancePredictor):
    """Variance predictor in :cite:t:`ren2020fastspeech` with residual F0 prediction

    Args:
        in_dim (int): the input dimension
        out_dim (int): the output dimension
        num_layers (int): the number of layers
        hidden_dim (int): the hidden dimension
        kernel_size (int): the kernel size
        dropout (float): the dropout rate
        in_lf0_idx (int): the index of the input LF0
        in_lf0_min (float): the minimum value of the input LF0
        in_lf0_max (float): the maximum value of the input LF0
        out_lf0_idx (int): the index of the output LF0
        out_lf0_mean (float): the mean value of the output LF0
        out_lf0_scale (float): the scale value of the output LF0
    """

    def __init__(
        self,
        in_dim,
        out_dim,
        num_layers=5,
        hidden_dim=256,
        kernel_size=5,
        dropout=0.5,
        # NOTE: you must carefully set the following parameters
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
    ):
        super().__init__(
            in_dim=in_dim,
            out_dim=out_dim,
            num_layers=num_layers,
            hidden_dim=hidden_dim,
            kernel_size=kernel_size,
            dropout=dropout,
        )
        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def forward(self, x, lengths=None, y=None):
        out = self.conv(x.transpose(1, 2))
        out = self.fc(out.transpose(1, 2))

        lf0_pred, lf0_residual = predict_lf0_with_residual(
            x,
            out,
            self.in_lf0_idx,
            self.in_lf0_min,
            self.in_lf0_max,
            self.out_lf0_idx,
            self.out_lf0_mean,
            self.out_lf0_scale,
        )

        # Inject the predicted lf0 into the output features
        out[:, :, self.out_lf0_idx] = lf0_pred.squeeze(-1)

        return out, lf0_residual

    def inference(self, x, lengths=None):
        return self(x, lengths)[0]


class MultistreamParametricModel(BaseModel):
    def __init__(
        self,
        energy_model: nn.Module,
        energy_stream_sizes: list,
        energy_has_dynamic_features: list,
        energy_num_windows: int,
        pitch_model: nn.Module,
        pitch_stream_sizes: list,
        pitch_has_dynamic_features: list,
        pitch_num_windows: int,
        timbre_model: nn.Module,
        timbre_stream_sizes: list,
        timbre_has_dynamic_features: list,
        timbre_num_windows: int,
        # NOTE: you must carefully set the following parameters
        in_lf0_idx=300,
        in_lf0_min=5.3936276,
        in_lf0_max=6.491111,
        out_lf0_idx=180,
        out_lf0_mean=5.953093881972361,
        out_lf0_scale=0.23435173188961034,
    ):
        super().__init__()
        self.energy_stream_sizes = energy_stream_sizes
        self.energy_has_dynamic_features = energy_has_dynamic_features
        self.energy_num_windows = energy_num_windows
        self.energy_model = energy_model
        if hasattr(self.energy_model, "out_dim"):
            assert self.energy_model.out_dim == sum(
                self.energy_stream_sizes
            ), "Energy model output dimension is not consistent with the stream sizes"

        self.pitch_stream_sizes = pitch_stream_sizes
        self.pitch_has_dynamic_features = pitch_has_dynamic_features
        self.pitch_num_windows = pitch_num_windows
        self.pitch_model = pitch_model
        if hasattr(self.pitch_model, "out_dim"):
            assert self.pitch_model.out_dim == sum(
                self.pitch_stream_sizes
            ), "Pitch model output dimension is not consistent with the stream sizes"

        self.timbre_stream_sizes = timbre_stream_sizes
        self.timbre_has_dynamic_features = timbre_has_dynamic_features
        self.timbre_num_windows = timbre_num_windows
        self.timbre_model = timbre_model
        if hasattr(self.timbre_model, "out_dim"):
            assert self.timbre_model.out_dim == sum(
                self.timbre_stream_sizes
            ), "Timbre model output dimension is not consistent with the stream sizes"

        self.in_lf0_idx = in_lf0_idx
        self.in_lf0_min = in_lf0_min
        self.in_lf0_max = in_lf0_max
        self.out_lf0_idx = out_lf0_idx
        self.out_lf0_mean = out_lf0_mean
        self.out_lf0_scale = out_lf0_scale

    def _set_lf0_params(self):
        # Special care for residual F0 prediction models
        # NOTE: don't overwrite out_lf0_idx
        if hasattr(self.pitch_model, "out_lf0_mean"):
            self.pitch_model.in_lf0_idx = self.in_lf0_idx
            self.pitch_model.in_lf0_min = self.in_lf0_min
            self.pitch_model.in_lf0_max = self.in_lf0_max
            self.pitch_model.out_lf0_mean = self.out_lf0_mean
            self.pitch_model.out_lf0_scale = self.out_lf0_scale

    def forward(self, x, lengths=None, y=None):
        self._set_lf0_params()

        out = self.energy_model(x, lengths)
        erg = split_streams(out, self.energy_stream_sizes)[0]

        # NOTE: so far assuming residual F0 prediction models
        out, lf0_residual = self.pitch_model(x, lengths)
        if len(self.pitch_stream_sizes) == 2:
            lf0, vuv = split_streams(out, self.pitch_stream_sizes)
        elif len(self.pitch_stream_sizes) == 3:
            lf0, vuv, vib = split_streams(out, self.pitch_stream_sizes)
        else:
            lf0, vuv, vib, vib_flags = split_streams(out, self.pitch_stream_sizes)

        out = self.timbre_model(x, lengths)
        mgc, bap = split_streams(out, self.timbre_stream_sizes)

        # concat mgcs' 0-th and rest dims
        mgc = torch.cat([erg, mgc], dim=-1)

        # make a concatenated stream
        if len(self.pitch_stream_sizes) == 2:
            out = torch.cat([mgc, lf0, vuv, bap], dim=-1)
        elif len(self.pitch_stream_sizes) == 3:
            out = torch.cat([mgc, lf0, vuv, bap, vib], dim=-1)
        else:
            out = torch.cat([mgc, lf0, vuv, bap, vib, vib_flags], dim=-1)

        return out, lf0_residual

    def inference(self, x, lengths=None):
        out, _ = self(x, lengths)
        return out

from .clustering_separation_base import ClusteringSeparationBase
import numpy as np
import torch
import librosa
from ..deep_mixin import DeepMixin
from .. import FT2D, Melodia, HPSS, Repet, RepetSim, MultichannelWienerFilter
from sklearn.decomposition import TruncatedSVD
from sklearn.preprocessing import scale
from copy import deepcopy

class SpatialClustering(ClusteringSeparationBase):
    def extract_features(self):
        ipd, ild = self.audio_signal.ipd_ild_features()
        num_channels = self.audio_signal.num_channels
        
        features = [
            [ipd for i in range(num_channels)],
            [ild for i in range(num_channels)]
        ]
        features = np.array(features).transpose(2, 3, 1, 0)   
        features = features.reshape(-1, features.shape[-1])   

        return features

class PrimitiveClustering(ClusteringSeparationBase):
    def __init__(
        self, 
        input_audio_signal,
        algorithms,
        num_cascades=1,
        **kwargs
    ):
        super().__init__(
            input_audio_signal,
            **kwargs
        )
        self.original_stft_params = deepcopy(self.audio_signal.stft_params)
        self.algorithms = [a[0] for a in algorithms]
        self.algorithm_parameters = [a[1] if len(a) > 1 else {} for a in algorithms]
        self.algorithm_returns = [a[2] if len(a) > 2 else [] for a in algorithms]
        self.num_cascades = num_cascades

    def run_algorithm_on_signal(self, mixture, level):
        separations = []
        separators = []
        for i, algorithm in enumerate(self.algorithms):
            stft_params = self.algorithm_parameters[i].pop('stft_params', None)
            if stft_params is not None:
                mixture.stft_data = None
                mixture.stft_params = stft_params
                
            separator = algorithm(
                mixture, 
                use_librosa_stft=self.use_librosa_stft, 
                **self.algorithm_parameters[i]
            )
            separator.run()
            signals = separator.make_audio_signals()
            if self.algorithm_returns[i]:
                signals = [signals[j] for j in self.algorithm_returns[i]]
            separations += signals
            separators.append(separator)
            mixture.stft_params = self.original_stft_params
            
        return separations, separators

    def extract_features_from_signals(self, signals):
        features = []
        self.audio_signal.stft_data = None
        self.audio_signal.stft_params = self.original_stft_params
        mix_stft = np.abs(self.audio_signal.stft())
        for s in signals:
            s.stft_data = None
            s.stft_params = self.original_stft_params
            _stft = np.abs(s.stft())
            _feature = _stft / np.maximum(_stft, mix_stft + 1e-7)
            features.append(_feature)
        features = np.array(features).transpose(1, 2, 3, 0)
        return features

    def extract_features_from_separators(self, separators):
        features = []
        for i, s in enumerate(separators):
            masks = [m.mask for m in s.result_masks]
            if self.algorithm_returns[i]:
                masks = [masks[j] for j in self.algorithm_returns[i]]
            features += masks
        features = np.array(features).transpose(1, 2, 3, 0)
        return features

    def extract_features(self):
        features = []
        current_signals = [self.audio_signal]
        separators = []
        for i in range(self.num_cascades):
            separations = []
            for signal in current_signals:
                _separations, _separator = self.run_algorithm_on_signal(signal, i)
                separations += _separations
                separators += _separator
            current_signals = separations
        self.separations = separations
        features = self.extract_features_from_separators(separators)
        self._compute_spectrograms()
        features = features.reshape(-1, features.shape[-1])
        #features = scale(features, axis=0)
        return features

class DeepClustering(ClusteringSeparationBase, DeepMixin):
    def __init__(
        self, 
        input_audio_signal,
        model_path,
        model=None,
        metadata=None,
        use_cuda=True,
        **kwargs
    ):
        
        self.device = torch.device(
            'cuda'
            if torch.cuda.is_available() and use_cuda
            else 'cpu'
        )
        
        self.model = model
        self.metadata = metadata
        self.model, self.metadata = self.load_model(model_path)

        input_audio_signal = self.set_audio_signal(input_audio_signal)

        sample_rate = self.metadata['sample_rate']
        num_mels = self.model.layers['mel_projection'].num_mels
        num_frequencies = (self.metadata['n_fft'] // 2) + 1
        filter_bank = None

        if num_mels > 0:
            weights = self.model.layers['mel_projection'].transform.weight.data.cpu().numpy()
            filter_bank = np.linalg.pinv(weights.T)

        self.filter_bank = filter_bank
        super().__init__(input_audio_signal, **kwargs)
        
    def set_audio_signal(self, new_audio_signal):
        input_audio_signal = deepcopy(new_audio_signal)
        if input_audio_signal.sample_rate != self.metadata['sample_rate']:
            input_audio_signal.resample(self.metadata['sample_rate'])
        input_audio_signal.stft_params.window_length = self.metadata['n_fft']
        input_audio_signal.stft_params.n_fft_bins = self.metadata['n_fft']
        input_audio_signal.stft_params.hop_length = self.metadata['hop_length']

        self.audio_signal = input_audio_signal
        self.original_length = input_audio_signal.signal_length
        self.original_sample_rate = input_audio_signal.sample_rate
        self.clusterer = None
        return input_audio_signal

    def postprocess(self, assignments, confidence):
        if self.filter_bank is not None:
            shape = (self.filter_bank.shape[0], -1, self.stft.shape[-1])
            assignments = assignments.reshape(shape + (self.num_sources,))
            confidence = confidence.reshape(shape)

            assignments = assignments.transpose()
            confidence = confidence.transpose()
                        
            assignments = np.dot(assignments, self.filter_bank) + 1e-6
            assignments = np.clip(assignments, 0.0, 1.0) 
            assignments /= np.sum(assignments, axis=0)

            confidence = np.dot(confidence, self.filter_bank)
            confidence += np.abs(confidence.min())
            
            assignments = assignments.transpose()
            confidence = confidence.transpose()
            assignments = assignments.transpose(3, 0, 1, 2)
        else:
            assignments, confidence = super().postprocess(assignments, confidence)

        return assignments, confidence

    def project_data(self, data):
        if self.model.layers['mel_projection'].num_mels > 0:
            data = self._format(data, 'rnn')
            data = torch.from_numpy(data).to(self.device).float()
            data = self.model.project_data(data, clamp=False)
            data = data.squeeze(-1).permute(2, 1, 0)
            data = (data > 0).cpu().data.numpy().astype(bool)
        return data

    def extract_features(self):
        input_data = self._preprocess()
        with torch.no_grad():
            output = self.model(input_data)
            if 'embedding' not in output:
                raise ValueError("This model is not a deep clustering model!")
            embedding = output['embedding']
            embedding_size = embedding.shape[-1]
            embedding = embedding.squeeze(-2)
            embedding = embedding.permute(2, 1, 0, 3)
            embedding = embedding.reshape(-1, embedding_size)
            embedding = embedding.data.cpu().numpy()
        return embedding

    def make_audio_signals(self):
        signals = super().make_audio_signals()
        residual = (self.audio_signal - sum(signals)).audio_data * (1 / len(signals))
        for signal in signals:
            signal.audio_data += residual
            if signal.sample_rate != self.original_sample_rate:
                signal.resample(self.original_sample_rate)
            signal.truncate_samples(self.original_length)
        return signals
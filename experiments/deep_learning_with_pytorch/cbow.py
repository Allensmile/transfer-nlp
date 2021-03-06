import logging
from typing import Dict, List, Any

import numpy as np
import pandas as pd
import torch

from transfer_nlp.common.tokenizers import CustomTokenizer
from transfer_nlp.embeddings.embeddings import Embedding
from transfer_nlp.loaders.loaders import DatasetSplits, DataFrameDataset
from transfer_nlp.loaders.vectorizers import Vectorizer
from transfer_nlp.loaders.vocabulary import CBOWVocabulary
from transfer_nlp.plugins.config import register_plugin
from transfer_nlp.plugins.predictors import PredictorABC

logger = logging.getLogger(__name__)


# Vectorizer
@register_plugin
class CBOWVectorizer(Vectorizer):

    def __init__(self, data_file: str):

        super().__init__(data_file=data_file)

        self.tokenizer = CustomTokenizer()
        df = pd.read_csv(data_file)

        data_vocab = CBOWVocabulary()
        max_context = 0
        for index, row in df.iterrows():
            tokens = self.tokenizer.tokenize(text=row.context)
            max_context = max(max_context, len(tokens))
            for token in tokens:
                data_vocab.add_token(token)
                data_vocab.add_token(row.target)
        self.data_vocab = data_vocab
        self.target_vocab = data_vocab
        self.max_context = max_context

    def vectorize(self, context: str) -> np.array:

        tokens = self.tokenizer.tokenize(text=context)
        indices = [self.data_vocab.lookup_token(token) for token in tokens]
        vector_length = self.max_context

        out_vector = np.zeros(vector_length, dtype=np.int64)
        out_vector[:len(indices)] = indices
        out_vector[len(indices):] = self.data_vocab.mask_index

        return out_vector


# Dataset
@register_plugin
class CBOWDataset(DatasetSplits):

    def __init__(self, data_file: str, batch_size: int, vectorizer: Vectorizer):
        self.df = pd.read_csv(data_file)

        # preprocessing
        self.vectorizer: Vectorizer = vectorizer

        self.df['x_in'] = self.df.apply(lambda row: self.vectorizer.vectorize(row.context), axis=1)
        self.df['y_target'] = self.df.apply(lambda row: self.vectorizer.target_vocab.lookup_token(row.target), axis=1)

        train_df = self.df[self.df.split == 'train'][['x_in', 'y_target']]
        val_df = self.df[self.df.split == 'val'][['x_in', 'y_target']]
        test_df = self.df[self.df.split == 'test'][['x_in', 'y_target']]

        super().__init__(train_set=DataFrameDataset(train_df), train_batch_size=batch_size,
                         val_set=DataFrameDataset(val_df), val_batch_size=batch_size,
                         test_set=DataFrameDataset(test_df), test_batch_size=batch_size)


@register_plugin
class CBOWClassifier(torch.nn.Module):  # Simplified cbow Model

    def __init__(self, data: DatasetSplits, embedding_size: int, glove_path: str = None, padding_idx: int = 0):
        super(CBOWClassifier, self).__init__()
        self.num_embeddings = len(data.vectorizer.data_vocab)
        self.embedding_size = embedding_size
        self.padding_idx = padding_idx

        if glove_path:
            logger.info("Using pre-trained word embeddings...")
            self.embeddings = Embedding(glove_filepath=glove_path, data=data).embeddings
            self.embeddings = torch.from_numpy(self.embeddings).float()
            glove_size = len(self.embeddings[0])
            self.embedding: torch.nn.Embedding = torch.nn.Embedding(embedding_dim=glove_size,
                                                                    num_embeddings=self.num_embeddings,
                                                                    padding_idx=self.padding_idx,
                                                                    _weight=self.embeddings)

        else:
            logger.info("Not using pre-trained word embeddings...")
            self.embedding: torch.nn.Embedding = torch.nn.Embedding(embedding_dim=self.embedding_size,
                                                                    num_embeddings=self.num_embeddings,
                                                                    padding_idx=self.padding_idx)

        self.fc1 = torch.nn.Linear(in_features=embedding_size,
                                   out_features=self.num_embeddings)
        self.dropout = torch.nn.Dropout(p=0.3)

    def forward(self, x_in: torch.Tensor) -> torch.Tensor:
        """

        :param x_in: input data tensor. x_in.shape should be (batch, input_dim)
        :param apply_softmax: flag for the softmax activation
                should be false if used with the Cross Entropy losses
        :return: the resulting tensor. tensor.shape should be (batch, output_dim)
        """

        x_embedded_sum = self.dropout(self.embedding(x_in).sum(dim=1))
        y_out = self.fc1(x_embedded_sum)

        return y_out


# Predictor
@register_plugin
class CBOWPredictor(PredictorABC):
    """
    Toy example: we want to make predictions on inputs of the form {"inputs": ["hello world", "foo", "bar"]}
    """

    def __init__(self, data: DatasetSplits, model: torch.nn.Module):
        super().__init__(vectorizer=data.vectorizer, model=model)

    def json_to_data(self, input_json: Dict) -> Dict:
        return {
            'x_in': torch.LongTensor([self.vectorizer.vectorize(context=input_string) for input_string in input_json['inputs']])}

    def output_to_json(self, outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "outputs": outputs}

    def decode(self, output: torch.tensor) -> List[Dict[str, Any]]:
        probabilities = torch.nn.functional.softmax(output, dim=1)
        probability_values, indices = probabilities.max(dim=1)

        return [{
            "class": self.vectorizer.data_vocab.lookup_index(index=int(res[1])),
            "probability": float(res[0])} for res in zip(probability_values, indices)]

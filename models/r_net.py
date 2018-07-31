import torch
from torch import nn
from models.submodules import PairEncoder, SelfMatchEncoder, CharLevelWordEmbeddingCnn, WordEmbedding, \
    SentenceEncoding, PointerNetwork
from modules.recurrent import AttentionEncoderCell


class RNet(nn.Module):
    def __init__(self,
                 args,
                 char_embedding_config,
                 word_embedding_config,
                 sentence_encoding_config,
                 pair_encoding_config,
                 self_matching_config,
                 pointer_config):
        super().__init__()
        self.word_embedding = WordEmbedding(
            word_embedding=word_embedding_config["embedding_weights"],
            padding_idx=word_embedding_config["padding_idx"],
            requires_grad=word_embedding_config["update"])

        self.char_embedding = CharLevelWordEmbeddingCnn(
            char_embedding_size=char_embedding_config["char_embedding_size"],
            char_num=char_embedding_config["char_num"],
            num_filters=char_embedding_config["num_filters"],
            ngram_filter_sizes=char_embedding_config["ngram_filter_sizes"],
            output_dim=char_embedding_config["output_dim"],
            activation=char_embedding_config["activation"],
            embedding_weights=char_embedding_config["embedding_weights"],
            padding_idx=char_embedding_config["padding_idx"],
            requires_grad=char_embedding_config["update"]
        )

        # we are going to concat the output of two embedding methods
        embedding_dim = self.word_embedding.output_dim + self.char_embedding.output_dim

        self.r_net = _RNet(args,
                           embedding_dim,
                           sentence_encoding_config,
                           pair_encoding_config,
                           self_matching_config,
                           pointer_config)

        self.args = args

    def forward(self,
                question,
                question_char,
                question_mask,
                passage,
                passage_char,
                passage_mask):
        # Embedding using Glove
        question, passage = self.word_embedding(question.tensor, passage.tensor)

        if torch.cuda.is_available():
            question_char = question_char.cuda(self.args.device_id)
            passage_char = passage_char.cuda(self.args.device_id)
            question = question.cuda(self.args.device_id)
            passage = passage.cuda(self.args.device_id)
            question_mask = question_mask.cuda(self.args.device_id)
            passage_mask = passage_mask.cuda(self.args.device_id)

        # char level embedding
        question_char = self.char_embedding(question_char.tensor)
        passage_char = self.char_embedding(passage_char.tensor)

        # concat word embedding and char level embedding
        passage = torch.cat([passage, passage_char], dim=-1)
        question = torch.cat([question, question_char], dim=-1)

        return self.r_net(passage, passage_mask, question, question_mask)

    def cuda(self, *args, **kwargs):
        self.r_net.cuda(*args, **kwargs)
        self.char_embedding.cuda(*args, **kwargs)
        return self


class _RNet(nn.Module):
    def __init__(self, args, input_size,
                 sentence_encoding_config,
                 pair_encoding_config,
                 self_matching_config, pointer_config):
        super().__init__()
        self.current_score = 0
        self.sentence_encoder = SentenceEncoding(
            input_size=input_size,
            hidden_size=sentence_encoding_config["hidden_size"],
            num_layers=sentence_encoding_config["num_layers"],
            bidirectional=sentence_encoding_config["bidirectional"],
            dropout=sentence_encoding_config["dropout"])

        sentence_encoding_direction = (2 if sentence_encoding_config["bidirectional"] else 1)
        sentence_encoding_size = (sentence_encoding_config["hidden_size"] * sentence_encoding_direction)
        self.pair_encoder = PairEncoder(
            question_size=sentence_encoding_size,
            passage_size=sentence_encoding_size,
            hidden_size=pair_encoding_config["hidden_size"],
            bidirectional=pair_encoding_config["bidirectional"],
            dropout=pair_encoding_config["dropout"],
            attention_size=pair_encoding_config["attention_size"]
        )

        pair_encoding_num_direction = (2 if pair_encoding_config["bidirectional"] else 1)
        pair_encoding_size = pair_encoding_config["hidden_size"] * pair_encoding_num_direction


        self.self_matching_encoder = SelfMatchEncoder(
            question_size=sentence_encoding_size,
            passage_size=pair_encoding_size,
            hidden_size=self_matching_config["hidden_size"],
            bidirectional=self_matching_config["bidirectional"],
            dropout=self_matching_config["dropout"],
            attention_size=self_matching_config["attention_size"]
        )

        passage_size = pair_encoding_num_direction * pair_encoding_config["hidden_size"]

        self.pointer_net = PointerNetwork(
            question_size=sentence_encoding_size,
            passage_size=passage_size,
            num_layers=pointer_config["num_layers"],
            dropout=pointer_config["dropout"],
            cell_type=pointer_config["rnn_cell"])

        for weight in self.parameters():
            if weight.ndimension() >= 2:
                nn.init.orthogonal(weight)

    def forward(self,
                question,
                question_mask,
                passage,
                passage_mask):
        # embed words using char-level and word-level and concat them
        question, passage = self.sentence_encoder(question, passage)
        passage = self.pair_encoder(question, question_mask, passage)
        passage = self.self_match_encode(passage, passage_mask, passage)
        begin, end = self.pointer_net(question,
                                      question_mask,
                                      passage,
                                      passage_mask)

        return begin, end
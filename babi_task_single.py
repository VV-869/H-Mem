#!/usr/bin/env python
"""Runs H-Mem on a single bAbI task."""

import argparse
import os
import random
from functools import reduce
from itertools import chain

import numpy as np
import tensorflow as tf
from data.babi_data import download, load_task, tasks, vectorize_data
from layers.encoding import Encoding
from layers.extracting import Extracting
from layers.reading import ReadingCell
from layers.writing import WritingCell
from tensorflow.keras import Model
from tensorflow.keras.layers import TimeDistributed
from utils.logger import MyCSVLogger

strategy = tf.distribute.MirroredStrategy()

parser = argparse.ArgumentParser()
parser.add_argument('--task_id', type=int, default=1)
parser.add_argument('--max_num_sentences', type=int, default=-1)
parser.add_argument('--training_set_size', type=str, default='10k', help='`1k` or `10k`')

parser.add_argument('--epochs', type=int, default=100)
parser.add_argument('--learning_rate', type=float, default=0.003)
parser.add_argument('--batch_size_per_replica', type=int, default=128)
parser.add_argument('--random_state', type=int, default=None)
parser.add_argument('--max_grad_norm', type=float, default=20.0)
parser.add_argument('--validation_split', type=float, default=0.1)

parser.add_argument('--hops', type=int, default=3)
parser.add_argument('--memory_size', type=int, default=100)
parser.add_argument('--embeddings_size', type=int, default=80)
parser.add_argument('--read_before_write', type=int, default=0)
parser.add_argument('--gamma_pos', type=float, default=0.01)
parser.add_argument('--gamma_neg', type=float, default=0.01)
parser.add_argument('--w_assoc_max', type=float, default=1.0)
parser.add_argument('--encodings_type', type=str, default='learned_encoding',
                    help='`identity_encoding`, `position_encoding` or `learned_encoding`')

parser.add_argument('--verbose', type=int, default=1)
parser.add_argument('--logging', type=int, default=0)
args = parser.parse_args()

batch_size = args.batch_size_per_replica * strategy.num_replicas_in_sync

# Set random seeds.
np.random.seed(args.random_state)
random.seed(args.random_state)
tf.random.set_seed(args.random_state)

if args.logging:
    logdir = 'results/'

    if not os.path.exists(logdir):
        os.makedirs(logdir)

# Download bAbI data set.
data_dir = download()

if args.verbose:
    print('Extracting stories for the challenge: {0}, {1}'.format(args.task_id, tasks[args.task_id]))

# Load the data.
train, test = load_task(data_dir, args.task_id, args.training_set_size)
data = train + test

vocab = sorted(reduce(lambda x, y: x | y, (set(list(chain.from_iterable(s)) + q + a) for s, q, a in data)))
word_idx = dict((c, i + 1) for i, c in enumerate(vocab))

max_story_size = max(map(len, (s for s, _, _ in data)))

max_num_sentences = max_story_size if args.max_num_sentences == -1 else min(args.max_num_sentences,
                                                                            max_story_size)

out_size = len(word_idx) + 1  # +1 for nil word.

# Add time words/indexes
for i in range(max_num_sentences):
    word_idx['time{}'.format(i+1)] = 'time{}'.format(i+1)

vocab_size = len(word_idx) + 1  # +1 for nil word.
mean_story_size = int(np.mean([len(s) for s, _, _ in data]))
max_sentence_size = max(map(len, chain.from_iterable(s for s, _, _ in data))) + 1  # +1 for time word.
max_query_size = max(map(len, (q for _, q, _ in data)))

if args.verbose:
    print('-')
    print('Vocab size:', vocab_size, 'unique words (including  "nil" word and "time" words)')
    print('Story max length:', max_story_size, 'sentences')
    print('Story mean length:', mean_story_size, 'sentences')
    print('Story max length:', max_sentence_size, 'words (including "time" word)')
    print('Query max length:', max_query_size, 'words')
    print('-')
    print('Here\'s what a "story" tuple looks like (story, query, answer):')
    print(data[0])
    print('-')
    print('Vectorizing the stories...')

# Vectorize the data.
max_words = max(max_sentence_size, max_query_size)
trainS, trainQ, trainA = vectorize_data(train, word_idx, max_num_sentences, max_words, max_words)
testS, testQ, testA = vectorize_data(test, word_idx, max_num_sentences, max_words, max_words)

trainQ = np.repeat(np.expand_dims(trainQ, axis=1), args.hops, axis=1)
testQ = np.repeat(np.expand_dims(testQ, axis=1), args.hops, axis=1)

story_shape = trainS.shape[1:]
query_shape = trainQ.shape[1:]

x_train = [trainS, trainQ]
y_train = np.argmax(trainA, axis=1)

x_test = [testS, testQ]
y_test = np.argmax(testA, axis=1)

if args.verbose:
    print('-')
    print('Stories: integer tensor of shape (samples, max_length, max_words): {0}'.format(trainS.shape))
    print('Here\'s what a vectorized story looks like (sentence, word):')
    print(trainS[0])
    print('-')
    print('Queries: integer tensor of shape (samples, length): {0}'.format(trainQ.shape))
    print('Here\'s what a vectorized query looks like:')
    print(trainQ[0])
    print('-')
    print('Answers: binary tensor of shape (samples, vocab_size): {0}'.format(trainA.shape))
    print('Here\'s what a vectorized answer looks like:')
    print(trainA[0])
    print('-')
    print('Training...')

with strategy.scope():
    # Build the model.
    story_input = tf.keras.layers.Input(story_shape, name='story_input')
    query_input = tf.keras.layers.Input(query_shape, name='query_input')

    embedding = tf.keras.layers.Embedding(input_dim=vocab_size,
                                          output_dim=args.embeddings_size,
                                          embeddings_initializer='he_uniform',
                                          embeddings_regularizer=None,
                                          mask_zero=True,
                                          name='embedding')
    story_embedded = TimeDistributed(embedding, name='story_embedding')(story_input)
    query_embedded = TimeDistributed(embedding, name='query_embedding')(query_input)

    encoding = Encoding(args.encodings_type, name='encoding')
    story_encoded = TimeDistributed(encoding, name='story_encoding')(story_embedded)
    query_encoded = TimeDistributed(encoding, name='query_encoding')(query_embedded)

    story_encoded = tf.keras.layers.BatchNormalization(name='batch_norm_story')(story_encoded)
    query_encoded = tf.keras.layers.BatchNormalization(name='batch_norm_query')(query_encoded)

    entities = Extracting(units=args.memory_size,
                          use_bias=False,
                          activation='relu',
                          kernel_initializer='he_uniform',
                          kernel_regularizer=tf.keras.regularizers.l2(1e-3),
                          name='entity_extracting')(story_encoded)

    memory_matrix = tf.keras.layers.RNN(WritingCell(units=args.memory_size,
                                                    read_before_write=args.read_before_write,
                                                    use_bias=False,
                                                    gamma_pos=args.gamma_pos,
                                                    gamma_neg=args.gamma_neg,
                                                    w_assoc_max=args.w_assoc_max,
                                                    kernel_initializer='he_uniform',
                                                    kernel_regularizer=tf.keras.regularizers.l2(1e-3)),
                                        name='entity_writing')(entities)

    queried_value = tf.keras.layers.RNN(ReadingCell(units=args.memory_size,
                                                    use_bias=False,
                                                    activation='relu',
                                                    kernel_initializer='he_uniform',
                                                    kernel_regularizer=tf.keras.regularizers.l2(1e-3)),
                                        name='entity_reading')(query_encoded, constants=[memory_matrix])

    outputs = tf.keras.layers.Dense(vocab_size,
                                    use_bias=False,
                                    kernel_initializer='he_uniform',
                                    name='output')(queried_value)

    model = Model(inputs=[story_input, query_input], outputs=outputs)

    # Compile the model.
    optimizer_kwargs = {'clipnorm': args.max_grad_norm} if args.max_grad_norm else {}
    model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=args.learning_rate, **optimizer_kwargs),
                  loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                  metrics=['accuracy'])

model.summary()


# Train and evaluate.
def lr_scheduler(epoch):
    if args.read_before_write:
        if epoch < 150:
            return args.learning_rate
        else:
            return args.learning_rate * tf.math.exp(0.01 * (150 - epoch))
    else:
        return args.learning_rate * 0.85**tf.math.floor(epoch / 20)


callbacks = []
callbacks.append(tf.keras.callbacks.LearningRateScheduler(lr_scheduler, verbose=0))
if args.logging:
    callbacks.append(tf.keras.callbacks.CSVLogger(os.path.join(logdir, '{0}_{1}_{2}_{3}-{4}.log'.format(
        args.task_id, args.training_set_size, args.encodings_type, args.hops, args.random_state))))

model.fit(x=x_train, y=y_train, epochs=args.epochs, validation_split=args.validation_split,
          batch_size=batch_size, callbacks=callbacks, verbose=args.verbose)

callbacks = []
if args.logging:
    callbacks.append(MyCSVLogger(os.path.join(logdir, '{0}_{1}_{2}_{3}-{4}.log'.format(
        args.task_id, args.training_set_size, args.encodings_type, args.hops, args.random_state))))

model.evaluate(x=x_test, y=y_test, callbacks=callbacks, verbose=2)

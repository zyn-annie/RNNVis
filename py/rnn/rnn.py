"""
RNN Model Classes
"""

import logging
import math
import time
import os

from py.datasets.data_utils import Feeder
from .command_utils import *
from .evaluator import Evaluator
from .trainer import Trainer

BasicRNNCell = tf.nn.rnn_cell.BasicRNNCell
BasicLSTMCell = tf.nn.rnn_cell.BasicLSTMCell
LSTMCell = tf.nn.rnn_cell.LSTMCell
GRUCell = tf.nn.rnn_cell.GRUCell
MultiRNNCell = tf.nn.rnn_cell.MultiRNNCell
DropOutWrapper = tf.nn.rnn_cell.DropoutWrapper
# EmbeddingWrapper = tf.nn.rnn_cell.EmbeddingWrapper
InputProjectionWrapper = tf.nn.rnn_cell.InputProjectionWrapper
OutputProjectionWrapper = tf.nn.rnn_cell.OutputProjectionWrapper

int32 = tf.int32


def sequence_loss(outputs, targets):
    """
    Weighted cross-entropy loss for a sequence of logits (per example).
    :param outputs: a 2D Tensor of shape [batch_size, output_size]
    :param targets: a 1D int32 Tensors of shape [batch_size]
    :return: a scalar tensor denoting the average loss of each example
    """
    if len(outputs.get_shape()) == 2:
        flatten_shape = tf.shape(targets)
    elif len(outputs.get_shape()) == 3:
        shape = outputs.get_shape().as_list()
        outputs = tf.reshape(outputs, [-1, shape[2]])
        targets = tf.reshape(targets, [-1])
        flatten_shape = tf.shape(targets)
    else:
        raise ValueError("outputs must be 2D or 3D tensor!")
    _loss = tf.nn.seq2seq.sequence_loss([outputs], [targets], [tf.ones(flatten_shape, dtype=data_type())])
    return _loss


class RNNModel(object):
    """
    A RNN Model unrolled from a RNN instance
    """
    def __init__(self, rnn, batch_size, num_steps, keep_prob=None, name=None):
        assert isinstance(rnn, RNN)
        # if batch_size is None:
        #     batch_size = tf.Variable(20, dtype=tf.int32, trainable=False)
        #     self._new_batch_size = tf.placeholder(tf.int32, shape=())
        #     self._update_batch_size = tf.assign(batch_size, self._new_batch_size, name="update_batch_size")
        self._rnn = rnn
        self._cell = rnn.cell
        self.batch_size = batch_size
        self.num_steps = num_steps
        self.name = name or "UnRolled"
        self.current_state = None
        # Ugly hackings for DropoutWrapper
        if keep_prob is not None:
            cell_list = [DropOutWrapper(cell, input_keep_prob=keep_prob, output_keep_prob=keep_prob) for cell in rnn.cell_list]
            self._cell = MultiRNNCell(cell_list, state_is_tuple=True)
        # The abstract name scope is not that easily dealt with, try to make the transparent to user
        with tf.name_scope(name):
            reuse = rnn.need_reuse
            with tf.variable_scope(rnn.name, reuse=reuse, initializer=rnn.initializer):
                # Build TF computation Graph
                input_shape = [None] + list(rnn.input_shape)[1:]
                target_shape = [None] + list(rnn.target_shape)[1:]
                self.state = self.cell.zero_state(batch_size, data_type())
                self.input_holders = tf.placeholder(rnn.input_dtype, [num_steps] + input_shape)
                self.target_holders = tf.placeholder(rnn.target_dtype, [num_steps] + target_shape)
                # ugly hacking for EmbeddingWrapper Badness
                self.inputs = self.input_holders if not rnn.has_embedding else rnn.map_to_embedding(self.input_holders)
                # Call TF api to create recurrent neural network
                self.outputs, self.final_state = \
                    tf.nn.dynamic_rnn(self.cell, self.inputs, initial_state=self.state, time_major=True)
                if rnn.has_projcet:
                    # rnn has output project, do manual projection for speed
                    outputs = tf.reshape(self.outputs, [-1, self.outputs.get_shape().as_list()[2]])
                    outputs = rnn.project_output(outputs)
                    self.targets = tf.reshape(self.target_holders, [-1])
                    self.loss = rnn.loss_func(outputs, self.targets)
                else:
                    self.loss = rnn.loss_func(self.outputs, self.target_holders)
        # Append self to rnn's model list
        rnn.models.append(self)

    @property
    def cell(self):
        return self._cell

    @property
    def rnn(self):
        return self._rnn

    def run(self, inputs, targets, epoch_size, run_ops, sess, eval_ops=None, verbose_every=None):
        """
        RNN Model's main API for running the part of graph in this model
        Note: this function can only be run after the graph is finalized
        :param inputs:
        :param targets:
        :param epoch_size:
        :param run_ops:
        :param eval_ops:
        :param sess:
        :param verbose:
        :return:
        """
        # initialize state and add ops that must be run
        if verbose_every is None or verbose_every is False:
            verbose_every = math.inf
        if self.current_state is None:
            self.init_state(sess)
        run_ops['state'] = self.final_state
        run_ops['loss'] = self.loss
        total_loss = 0
        vals = None
        start_time = verbose_time = time.time()
        evals = []
        for i in range(epoch_size):
            feed_dict = self.feed_state(self.current_state)
            if isinstance(inputs, Feeder):
                _inputs = inputs()
                _targets = targets()
            else:
                _inputs, _targets = sess.run([inputs, targets])
            feed_dict.update(self.feed_data(_inputs, True))
            feed_dict.update(self.feed_data(_targets, False))
            # feed_dict[model.target_holders] = [targets[:, i] for i in range(model.num_steps)]
            vals = sess.run(run_ops, feed_dict)
            self.current_state = vals['state']
            total_loss += vals['loss']
            if eval_ops:
                evals.append(sess.run(eval_ops, feed_dict))
            if i % verbose_every == 0 and i!=0:
                delta_time = time.time() - verbose_time
                print("epoch[{:d}/{:d}] avg loss:{:.3f}, speed:{:.1f} wps, time: {:.1f}s".format(
                    i, epoch_size, total_loss / i,
                                   i * self.batch_size * self.num_steps / (time.time() - start_time), delta_time))
                verbose_time = time.time()
        total_time = time.time() - start_time
        # Prepare for returning values
        vals['loss'] = total_loss / epoch_size
        vals['time'] = total_time
        if eval_ops:
            vals['evals'] = evals
        if math.isfinite(verbose_every):
            print("Epoch Summary: avg loss:{:.3f}, total time:{:.1f}s, speed:{:.1f} wps".format(
                total_loss / epoch_size, total_time, epoch_size * self.num_steps * self.batch_size / total_time))
        return vals

    def feed_state(self, state):
        """
        Create feed_dict specifying the state of rnn input state
        :param state: a tuple of states which can be convert to numpy arrays
        :return: a feed_dict used in sess.run()
        """
        # state_size = self._rnn.cell.state_size
        feed_dict = {}
        # if tf.nest.is_sequence(state_size): For multiRNNCell, the state must be a tuple
        # state is tuple
        for i, s in enumerate(self.state):
            # s is tuple
            if isinstance(s, tf.nn.rnn_cell.LSTMStateTuple):
                feed_dict[s.c] = state[i].c
                feed_dict[s.h] = state[i].h
            else:
                feed_dict[s] = state[i]
        return feed_dict

    def feed_data(self, data, inputs=True):
        holders = self.input_holders if inputs else self.target_holders
        return {holders: data}

    def init_state(self, sess):
        self.current_state = sess.run(self.state)
        return self.current_state

    def log_ops_placement(self, ops, log_path):
        # Print ops assignments for debugging
        sess = tf.Session(config=tf.ConfigProto(log_device_placement=True))
        _hdlr = tf.logging._handler
        new_handler = logging.FileHandler(log_path, 'a+')
        tf.logging._logger.removeHandler(_hdlr)
        tf.logging._logger.addHandler(new_handler)
        try:
            sess.run(ops)
        except:
            print("ops placements end.")
        tf.logging._logger.removeHandler(new_handler)
        tf.logging._logger.addHandler(_hdlr)
        print("ops placements info logged to {}".format(log_path))
    # def update_batch_size(self, new_batch_size, sess):
    #     sess.run(self._update_batch_size, {self._new_batch_size: new_batch_size})


class RNN(object):
    """
    A helper class that wraps TF RNNCells
    This class is used for wrapping RNN Model definition. You cannot use this class directly for computation.
    For computation (training, evaluating), use RNN.unroll() to create RNNModel,
    which create TF computation Graph for computation.
    """
    def __init__(self, name="RNN", initializer=None, logdir=None, graph=None):
        """
        :param name: a str, used to create variable scope
        :return: a empty RNN model
        """
        self.name = name
        self.initializer = initializer if initializer is not None else tf.random_uniform_initializer(-0.1,0.1)
        self.input_shape = None
        self.input_dtype = None
        self.output_shape = None
        self.output_dtype = None
        self.target_shape = None
        self.target_dtype = None

        self._cell = None
        self.cell_list = []
        self.trainer = None
        self.evaluator = None
        self.validator = None
        self.loss_func = None
        self.is_compiled = False
        self.embedding_size = None
        self.vocab_size = None
        self.supervisor = None
        self.models = []
        self.graph = graph if isinstance(graph, tf.Graph) else tf.get_default_graph()
        self.logdir = logdir or name

    def set_input(self, dshape, dtype, vocab_size, embedding_size=None):
        """
        set the input data shape of the model
        :param dshape: tuple or a list, [batch_size, ...]
            typically [None] for word-id input,
            for word embeddings, should be [None, feature_dim]
        :param dtype: tensorflow data type
        :return: None
        """
        assert not self.is_compiled
        self.input_shape = list(dshape)
        self.input_dtype = dtype
        self.embedding_size = embedding_size
        self.vocab_size = vocab_size

    def set_output(self, dshape, dtype=tf.float32):
        """
        Set the target data shape and type
        :param dshape: tuple or a list, [batch_size, ...],
            typically, for language modeling problem, the shape should match the input shape
        :param dtype: tensorflow data type
        :return: None
        """
        assert not self.is_compiled
        self.output_shape = list(dshape)
        self.output_dtype = dtype

    def set_target(self, dshape, dtype=tf.int32):
        assert not self.is_compiled
        self.target_shape = dshape
        self.target_dtype = dtype

    def set_loss_func(self, loss_func):
        """
        Set the loss function of the model
        :param loss_func: a function of form loss = loss_func(outputs, targets), where loss is a scalar Tensor
        :return: None
        """
        assert not self.is_compiled
        self.loss_func = loss_func

    def add_cell(self, cell, *args, **kwargs):
        """
        A helper function to use for create RNN cells under specific TensorFlow VariableScope
        :param cell: should be subclasses of tf.nn.rnn_cell.BasicRNNCell
        :param name: the name or scope used for managing the cell
        :param kwargs: the arguments used to create the cell
        :return: a cell managed under scope
        """
        assert not self.is_compiled
        self.cell_list.append(cell(*args, **kwargs))

    def compile(self, evaluate=True):
        """
        Compile the model. Should be called before training or running the model.
        Basically, this function just do checkings on model configurations,
            and create a Evaluator which contains an unrolled model
        :return: None
        """
        if self.is_compiled:  # In case of multiple compiles
            print("Already compiled!")
            return
        if self.input_shape is None or self.input_dtype is None:
            raise ValueError("input_shape or input_dtype is None, call set_input first!")
        if self.output_shape is None or self.output_dtype is None:
            raise ValueError("output_shape or output_dtype is None, call set_output first!")
        if self.target_shape is None or self.target_dtype is None:
            raise ValueError("target_shape or target_dtype is None, call set_target first!")
        if self.loss_func is None:
            raise ValueError("loss_func is None, call set_loss_func first!")
        # This operation creates no tf.Variables, no need for using variable scope
        self._cell = tf.nn.rnn_cell.MultiRNNCell(cells=self.cell_list)
        # All done
        self.is_compiled = True
        # Create a default evaluator
        if evaluate:
            with self.graph.as_default():
                with tf.device("/cpu:0"):
                    self.evaluator = Evaluator(self, batch_size=1)

    def unroll(self, batch_size, num_steps, keep_prob=None, name=None):
        """
        Unroll the RNN Cell into a RNN Model with fixed num_steps and batch_size
        :param batch_size: batch_size of the model
        :param num_steps: unrolled num_steps of the model
        :param keep_prob: keep probability in training (like dropout in CNN)
        :param name: name of the model
        :return:
        """
        assert self.is_compiled
        with self.graph.as_default():  # Ensure that the model is created under the managed graph
            return RNNModel(self, batch_size, num_steps, keep_prob=keep_prob, name=name)

    def add_trainer(self, batch_size, num_steps, keep_prob=1.0, optimizer="GradientDescent", learning_rate=1.0, clipper=None):
        """
        The previous version of train is too messy, explicitly break them into add_trainer, and train
        add_trainer does all the preparations on building TF graphs, this function should be called before train
        :param batch_size: batch_size of the running
        :param num_steps: unrolled num_steps
        :param keep_prob: see unroll
        :param optimizer: the optimizer to use, can be a str indicating the tf optimizer, or a subclass of tf.train.Optimizer
        :param learning_rate:
        :param clipper:
        :return:
        """
        assert self.is_compiled
        if self.trainer is not None:
            print("trainer is already exists! Currently do not support multi training!")
            return
        with self.graph.as_default():
            self.trainer = Trainer(self, batch_size, num_steps, keep_prob, optimizer, learning_rate, clipper)

    def add_validator(self, batch_size, num_steps):
        assert self.is_compiled
        if self.validator is not None:
            print("validator is already exists! Currently do not support multi training!")
            return
        with self.graph.as_default():
            self.validator = Evaluator(self, batch_size, num_steps, False, False, False)

    def add_evaluator(self, batch_size=1, record_every=1, log_state=True, log_input=True, log_output=True):
        """
        Explicitly add evaluator instead of using the default one. You must explicitly call compile(evaluate=False)
            before calling this function
        :param batch_size: default to be 1
        :param record_every: record frequency
        :param log_state: flag of state logging
        :param log_input: flag of input logging
        :param log_output: flag of output logging
        :param logdir: logging directory
        :return:
        """
        assert self.evaluator is None
        with self.graph.as_default():
            with tf.device("/cpu:0"):
                self.evaluator = Evaluator(self, batch_size, record_every, log_state, log_input, log_output)

    def train(self, inputs, targets, epoch_size, epoch_num, valid_inputs=None, valid_targets=None,
              valid_epoch_size=None, verbose=True):
        """
        Training using given input and target data
        TODO: Clean up this messy function
        :param inputs: should be a list of input sequence, each element of the list is a input sequence specified by x.
        :param targets: should be of size [num_seq, seq_length, ...]
        :param epoch_size: the size of an epoch
        :param epoch_num: number of training epochs
        :param valid_inputs: Validation input data
        :param valid_targets: Validation target data
        :param valid_epoch_size: batch_size of validation set
        :param verbose: Print training information if True
        :return: None
        """
        assert self.is_compiled
        assert self.trainer is not None
        with self.graph.as_default():
            if valid_inputs is None:
                # Only needs to run training graph
                self.finalize()
                print("Start Running Train Graph")
                self.trainer.train(inputs, targets, epoch_size, epoch_num, self.supervisor)
            else:
                self.finalize()
                print("Start Running Train Graph")
                with self.supervisor.managed_session(config=config_proto) as sess:
                    for i in range(epoch_num):
                        if verbose:
                            print("Epoch {}:".format(i))
                        self.trainer.train_one_epoch(inputs, targets, epoch_size, sess, verbose=verbose)
                        self.validator.evaluate(valid_inputs, valid_targets, valid_epoch_size, sess, verbose=False)

    def evaluate(self, inputs, targets, epoch_size, logdir=None):
        assert self.is_compiled
        with self.graph.as_default():
            self.finalize()
            with self.supervisor.managed_session(config=config_proto) as sess:
                logdir = os.path.join(self.logdir, "./evaluate") if logdir is None else logdir
                self.evaluator.evaluate(inputs, targets, epoch_size, sess, record=True, verbose=True, logdir=logdir)

    def save(self, path=None):
        """
        Save the model to a given path
        :param path:
        :return:
        """
        if not self.finalized:
            self.finalize()
        path = path if path is not None else os.path.join(self.logdir, './model')
        with self.supervisor.managed_session() as sess:
            self.supervisor.saver.save(sess, path, global_step=self.supervisor.global_step)
            print("Model variables saved to {}.".format(path))

    def restore(self, path=None):
        if not self.finalized:
            self.finalize()
        path = path if path is not None else self.logdir
        checkpoint = tf.train.latest_checkpoint(path)
        print(path)
        print(checkpoint)
        with self.supervisor.managed_session() as sess:
            self.supervisor.saver.restore(sess, checkpoint)
        print("Model variables restored from {}.".format(path))

    def finalize(self):
        """
        After all the computation ops are built in the graph, build a supervisor which implicitly finalize the graph
        :return: None
        """
        if self.finalized:
            print("Graph has already been finalized!")
            return False
        self.supervisor = tf.train.Supervisor(self.graph, logdir=self.logdir)
        return True

    @property
    def finalized(self):
        return False if self.supervisor is None else False

    @property
    def cell(self):
        return self._cell

    @property
    def need_reuse(self):
        return None if len(self.models) == 0 else True

    @property
    def has_embedding(self):
        return bool(self.embedding_size)

    @property
    def has_projcet(self):
        return self.cell_list[-1].output_size != self.output_shape[-1]

    def map_to_embedding(self, inputs):
        """
        Map the input ids into embedding
        :param inputs: a 2D Tensor of shape (num_steps, batch_size) of type int32, denoting word ids
        :return: a 3D Tensor of shape (num_Steps, batch_size, embedding_size) of type float32.
        """
        if self.has_embedding:
            # The Variables are already created in the compile(), need to
            with tf.variable_scope('embedding', initializer=self.initializer):
                with tf.device("/cpu:0"):  # Force CPU since GPU implementation is missing
                    embedding = tf.get_variable("embedding", [self.vocab_size, self.embedding_size], dtype=data_type())
                    return tf.nn.embedding_lookup(embedding, inputs)
        else:
            return None

    def project_output(self, outputs):
        """
        Project outputs into softmax distributions
        :param outputs: a tensor of shape (batch_size, output_size)
        :return: softmax distributions on vocab_size, a Tensor of shape (batch_size, vocab_size)
        """
        if self.has_projcet:
            with tf.variable_scope('project', initializer=self.initializer):
                project_w = tf.get_variable(
                    "project_w", [self.cell_list[-1].output_size, self.vocab_size], dtype=data_type())
                projcet_b = tf.get_variable("project_b", [self.vocab_size], dtype=data_type())
                return tf.matmul(outputs, project_w) + projcet_b
        else:
            return None

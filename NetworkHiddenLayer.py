
import theano
import numpy
import json
from theano import tensor as T
from theano.tensor.nnet import conv
from theano.tensor.signal import downsample
from NetworkBaseLayer import Layer
from ActivationFunctions import strtoact, strtoact_single_joined, elu
import TheanoUtil
from TheanoUtil import class_idx_seq_to_1_of_k, windowed_batch
from Log import log


class HiddenLayer(Layer):
  def __init__(self, activation="sigmoid", **kwargs):
    """
    :type activation: str | list[str]
    """
    super(HiddenLayer, self).__init__(**kwargs)
    self.set_attr('activation', activation.encode("utf8"))
    self.activation = strtoact(activation)
    self.W_in = [self.add_param(self.create_forward_weights(s.attrs['n_out'],
                                                            self.attrs['n_out'],
                                                            name="W_in_%s_%s" % (s.name, self.name)))
                 for s in self.sources]
    self.set_attr('from', ",".join([s.name for s in self.sources]))

  def get_linear_forward_output(self):
    z = self.b
    assert len(self.sources) == len(self.masks) == len(self.W_in)
    for s, m, W_in in zip(self.sources, self.masks, self.W_in):
      if s.attrs['sparse']:
        if s.output.ndim == 3: out_dim = s.output.shape[2]
        elif s.output.ndim == 2: out_dim = 1
        else: assert False, s.output.ndim
        z += W_in[T.cast(s.output, 'int32')].reshape((s.output.shape[0],s.output.shape[1],out_dim * W_in.shape[1]))
      elif m is None:
        z += self.dot(s.output, W_in)
      else:
        z += self.dot(self.mass * m * s.output, W_in)
    return z


class ForwardLayer(HiddenLayer):
  layer_class = "hidden"

  def __init__(self, sparse_window = 1, **kwargs):
    super(ForwardLayer, self).__init__(**kwargs)
    self.set_attr('sparse_window', sparse_window) # TODO this is ugly
    self.attrs['n_out'] = sparse_window * kwargs['n_out']
    self.z = self.get_linear_forward_output()
    self.make_output(self.z if self.activation is None else self.activation(self.z))


class EmbeddingLayer(ForwardLayer):
  layer_class = "embedding"

  def __init__(self, **kwargs):
    super(EmbeddingLayer, self).__init__(**kwargs)
    self.z -= self.b
    self.make_output(self.z if self.activation is None else self.activation(self.z))

class _NoOpLayer(Layer):
  """
  Use this as a base class if you want to remove all params by the Layer base class.
  Note that this overwrites n_out, so take care of that yourself.
  """
  def __init__(self, **kwargs):
    # The base class will already have a bias.
    # We will reset all this.
    # This is easier for now than to refactor the ForwardLayer.
    kwargs['n_out'] = 1  # This is a hack so that the super init is fast. Will be reset later.
    super(_NoOpLayer, self).__init__(**kwargs)
    self.params = {}  # Reset all params.
    self.set_attr('from', ",".join([s.name for s in self.sources]))


def concat_sources(sources, masks=None, mass=None, unsparse=False, expect_source=True):
  """
  :type sources: list[Layer]
  :type masks: None | list[theano.Variable]
  :type mass: None | theano.Variable
  :param bool unsparse: whether to make sparse sources into 1-of-k
  :param bool expect_source: whether to throw an exception if there is no source
  :returns (concatenated sources, out dim)
  :rtype: (theano.Variable, int)
  """
  if masks is None: masks = [None] * len(sources)
  else: assert mass
  assert len(sources) == len(masks)
  zs = []
  n_out = 0
  have_sparse = False
  have_non_sparse = False
  for s, m in zip(sources, masks):
    if s.attrs['sparse']:
      if s.output.ndim == 3: out = s.output.reshape((s.output.shape[0], s.output.shape[1]))
      elif s.output.ndim == 2: out = s.output
      else: assert False, s.output.ndim
      if unsparse:
        n_out += s.attrs['n_out']
        have_non_sparse = True
        out_1_of_k = class_idx_seq_to_1_of_k(out, num_classes=s.attrs['n_out'])
        zs += [out_1_of_k]
      else:
        zs += [out.reshape((out.shape[0], out.shape[1], 1))]
        assert not have_non_sparse, "mixing sparse and non-sparse sources"
        if not have_sparse:
          have_sparse = True
          n_out = s.attrs['n_out']
        else:
          assert n_out == s.attrs['n_out'], "expect same num labels but got %i != %i" % (n_out, s.attrs['n_out'])
    else:  # non-sparse source
      n_out += s.attrs['n_out']
      have_non_sparse = True
      assert not have_sparse, "mixing sparse and non-sparse sources"
      if m is None:
        zs += [s.output]
      else:
        zs += [mass * m * s.output]
  if len(zs) > 1:
    # We get (time,batch,dim) input shape.
    # Concat over dimension, axis=2.
    return T.concatenate(zs, axis=2), n_out
  elif len(zs) == 1:
    return zs[0], n_out
  else:
    if expect_source:
      raise Exception("We expected at least one source but did not get any.")
    return None, 0


class CopyLayer(_NoOpLayer):
  """
  It's mostly the Identity function. But it will make sparse to non-sparse.
  """
  layer_class = "copy"

  def __init__(self, activation=None, **kwargs):
    super(CopyLayer, self).__init__(**kwargs)
    if activation:
      self.set_attr('activation', activation.encode("utf8"))
    act_f = strtoact_single_joined(activation)

    self.z, n_out = concat_sources(self.sources, masks=self.masks, mass=self.mass, unsparse=True)
    self.set_attr('n_out', n_out)
    self.make_output(act_f(self.z))


class WindowLayer(_NoOpLayer):
  layer_class = "window"

  def __init__(self, window, **kwargs):
    super(WindowLayer, self).__init__(**kwargs)
    source, n_out = concat_sources(self.sources, unsparse=False)
    self.set_attr('n_out', n_out * window)
    self.set_attr('window', window)
    self.make_output(windowed_batch(source, window=window))


class DownsampleLayer(_NoOpLayer):
  """
  E.g. method == "average", axis == 0, factor == 2 -> each 2 time-frames are averaged.
  See TheanoUtil.downsample. You can also use method == "max".
  """
  layer_class = "downsample"

  def __init__(self, factor, axis, method="average", **kwargs):
    super(DownsampleLayer, self).__init__(**kwargs)
    self.set_attr("method", method)
    if isinstance(axis, (str, unicode)):
      axis = json.loads(axis)
    if isinstance(axis, set): axis = tuple(axis)
    assert isinstance(axis, int) or isinstance(axis, (tuple, list)), "int or list[int] expected for axis"
    if isinstance(axis, int): axis = [axis]
    axis = list(sorted(axis))
    self.set_attr("axis", axis)
    if isinstance(factor, (str, unicode)):
      factor = json.loads(factor)
    assert isinstance(factor, (int, float)) or isinstance(axis, (tuple, list)), "int|float or list[int|float] expected for factor"
    if isinstance(factor, (int, float)): factor = [factor] * len(axis)
    assert len(factor) == len(axis)
    self.set_attr("factor", factor)
    z, z_dim = concat_sources(self.sources, unsparse=False)
    n_out = z_dim
    for f, a in zip(factor, axis):
      z = TheanoUtil.downsample(z, axis=a, factor=f, method=method)
      if a == 0:
        self.index = TheanoUtil.downsample(self.sources[0].index, axis=0, factor=f, method="min")
      elif a == 2:
        n_out = int(n_out / f)
    output = z
    if method == 'concat':
      n_out *= numpy.prod(factor)
    elif method == 'lstm':
      num_batches = z.shape[2]
      #z = theano.printing.Print("a", attrs=['shape'])(z)
      z = z.dimshuffle(1,0,2,3).reshape((z.shape[1],z.shape[0]*z.shape[2],z.shape[3]))
      #z = theano.printing.Print("b", attrs=['shape'])(z)
      from math import sqrt
      from ActivationFunctions import elu
      l = sqrt(6.) / sqrt(6 * n_out)
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(n_out, n_out)), dtype=theano.config.floatX)
      self.A_in = self.add_param(theano.shared(value=values, borrow=True, name = "A_in_" + self.name))
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(n_out, n_out)), dtype=theano.config.floatX)
      self.A_re = self.add_param(theano.shared(value=values, borrow=True, name = "A_re_" + self.name))
      def lstmk(z_t, y_p, c_p):
        z_t += T.dot(y_p, self.A_re)
        partition = z_t.shape[1] / 4
        ingate = T.nnet.sigmoid(z_t[:,:partition])
        forgetgate = T.nnet.sigmoid(z_t[:,partition:2*partition])
        outgate = T.nnet.sigmoid(z_t[:,2*partition:3*partition])
        input = T.tanh(z_t[:,3*partition:4*partition])
        c_t = forgetgate * c_p + ingate * input
        y_t = outgate * T.tanh(c_t)
        return (y_t, c_t)
      def attent(xt, yp, W_re):
        return T.tanh(xt + elu(T.dot(yp, W_re)))
        #return T.tanh(T.dot(xt, W_in) + T.dot(yp, W_re))
      z, _ = theano.scan(attent, sequences = T.dot(z,self.A_in), outputs_info = [T.zeros_like(z[0])], non_sequences=[self.A_re])
      #result, _ = theano.scan(lstmk, sequences = T.dot(z,self.A_in), outputs_info = [T.zeros_like(z[0]),T.zeros_like(z[0])])
      #z = result[0]
      #from OpLSTM import LSTMOpInstance
      #inp = T.alloc(numpy.cast[theano.config.floatX](0), z.shape[0], z.shape[1], z.shape[2] * 4) + T.dot(z,self.A_in)
      #sta = T.alloc(numpy.cast[theano.config.floatX](0), z.shape[1], z.shape[2])
      #idx = T.alloc(numpy.cast[theano.config.floatX](1), z.shape[0], z.shape[1])
      #result = LSTMOpInstance(inp, self.A_re, sta, idx)
      #result = LSTMOpInstance(T.dot(z,self.A_in), self.A_re, T.zeros_like(z[0]), T.ones_like(z[:,:,0]))
      output = z[-1].reshape((z.shape[1] / num_batches, num_batches, z.shape[2]))
      #output = result[0][0].reshape((z.shape[1] / num_batches, num_batches, z.shape[2]))
    elif method == 'batch':
      self.index = TheanoUtil.downsample(self.sources[0].index, axis=0, factor=factor[0], method="batch")
      #z = theano.printing.Print("d", attrs=['shape'])(z)
    self.set_attr('n_out', n_out)
    self.make_output(output)


class UpsampleLayer(_NoOpLayer):
  layer_class = "upsample"

  def __init__(self, factor, axis, time_like_last_source=False, method="nearest-neighbor", **kwargs):
    super(UpsampleLayer, self).__init__(**kwargs)
    self.set_attr("method", method)
    self.set_attr("time_like_last_source", time_like_last_source)
    if isinstance(axis, (str, unicode)):
      axis = json.loads(axis)
    if isinstance(axis, set): axis = tuple(axis)
    assert isinstance(axis, int) or isinstance(axis, (tuple, list)), "int or list[int] expected for axis"
    if isinstance(axis, int): axis = [axis]
    axis = list(sorted(axis))
    self.set_attr("axis", axis)
    if isinstance(factor, (str, unicode)):
      factor = json.loads(factor)
    assert isinstance(factor, (int, float)) or isinstance(axis, (tuple, list)), "int|float or list[int|float] expected for factor"
    if isinstance(factor, (int, float)): factor = [factor] * len(axis)
    assert len(factor) == len(axis)
    self.set_attr("factor", factor)
    sources = self.sources
    assert len(sources) > 0
    if time_like_last_source:
      assert len(sources) >= 2
      source_for_time = sources[-1]
      sources = sources[:-1]
    else:
      source_for_time = None
    z, z_dim = concat_sources(sources, unsparse=False)
    n_out = z_dim
    for f, a in zip(factor, axis):
      target_axis_len = None
      if a == 0:
        assert source_for_time, "not implemented yet otherwise. but this makes most sense anyway."
        self.index = source_for_time.index
        target_axis_len = self.index.shape[0]
      elif a == 2:
        n_out = int(n_out * f)
      z = TheanoUtil.upsample(z, axis=a, factor=f, method=method, target_axis_len=target_axis_len)
    self.set_attr('n_out', n_out)
    self.make_output(z)


class FrameConcatZeroLayer(_NoOpLayer): # TODO: This is not correct for max_seqs > 1
  """
  Concats zero at the start (left=True) or end in the time-dimension.
  I.e. you can e.g. delay the input by N frames.
  See also FrameConcatZeroLayer (frame_cutoff).
  """
  layer_class = "frame_concat_zero"

  def __init__(self, num_frames, left=True, **kwargs):
    super(FrameConcatZeroLayer, self).__init__(**kwargs)
    self.set_attr("num_frames", num_frames)
    self.set_attr("left", left)
    assert len(self.sources) == 1
    s = self.sources[0]
    for attr in ["n_out", "sparse"]:
      self.set_attr(attr, s.attrs[attr])
    inp = s.output
    # We get (time,batch,dim) input shape.
    time_shape = [inp.shape[i] for i in range(1, inp.ndim)]
    zeros_shape = [num_frames] + time_shape
    zeros = T.zeros(zeros_shape, dtype=inp.dtype)
    if left:
      self.output = T.concatenate([zeros, inp], axis=0)
      self.index = T.concatenate([T.repeat(s.index[:1], num_frames, axis=0), s.index], axis=0)
    else:
      self.output = T.concatenate([inp, zeros], axis=0)
      self.index = T.concatenate([s.index, T.repeat(s.index[-1:], num_frames, axis=0)], axis=0)


class FrameCutoffLayer(_NoOpLayer): # TODO: This is not correct for max_seqs > 1
  """
  Cutoffs frames at the start (left=True) or end in the time-dimension.
  You should use this when you used FrameConcatZeroLayer(frame_concat_zero).
  """
  layer_class = "frame_cutoff"

  def __init__(self, num_frames, left=True, **kwargs):
    super(FrameCutoffLayer, self).__init__(**kwargs)
    self.set_attr("num_frames", num_frames)
    self.set_attr("left", left)
    assert len(self.sources) == 1
    s = self.sources[0]
    for attr in ["n_out", "sparse"]:
      self.set_attr(attr, s.attrs[attr])
    if left:
      self.output = s.output[num_frames:]
      self.index = s.index[num_frames:]
    else:
      self.output = s.output[:-num_frames]
      self.index = s.index[:-num_frames]


class ReverseLayer(_NoOpLayer):
  """
  Reverses the time-dimension.
  """
  layer_class = "reverse"

  def __init__(self, **kwargs):
    super(ReverseLayer, self).__init__(**kwargs)
    assert len(self.sources) == 1
    s = self.sources[0]
    for attr in ["n_out", "sparse"]:
      self.set_attr(attr, s.attrs[attr])
    # We get (time,batch,dim) input shape.
    self.index = s.index[::-1]
    self.output = s.output[::-1]


class CalcStepLayer(_NoOpLayer):
  layer_class = "calc_step"

  def __init__(self, n_out=None, from_prev="", apply=False, step=None, initial="zero", **kwargs):
    super(CalcStepLayer, self).__init__(**kwargs)
    if n_out is not None:
      self.set_attr("n_out", n_out)
    if from_prev:
      self.set_attr("from_prev", from_prev.encode("utf8"))
    self.set_attr("apply", apply)
    if step is not None:
      self.set_attr("step", step)
    self.set_attr("initial", initial.encode("utf8"))
    if not apply:
      assert n_out is not None
      assert self.network
      if self.network.calc_step_base:
        prev_layer = self.network.calc_step_base.get_layer(from_prev)
        if not prev_layer:
          self.network.calc_step_base.print_network_info("Prev-Calc-Step network")
          raise Exception("%s not found in prev calc step network" % from_prev)
        assert n_out == prev_layer.attrs["n_out"]
        self.output = prev_layer.output
      else:
        # First calc step. Just use zero.
        shape = [self.index.shape[0], self.index.shape[1], n_out]
        if initial == "zero":
          self.output = T.zeros(shape, dtype="float32")
        elif initial == "param":
          values = numpy.asarray(self.rng.normal(loc=0.0, scale=numpy.sqrt(12. / n_out), size=(n_out,)), dtype="float32")
          initial_param = self.add_param(theano.shared(value=values, borrow=True, name="output_initial"))
          self.output = initial_param.dimshuffle('x', 'x', 0)
        else:
          raise Exception("CalcStepLayer: initial %s invalid" % initial)
    else:
      assert step is not None
      assert len(self.sources) == 1
      assert not from_prev
      # We will refer to the previous calc-step layer this way
      # so that we ensure that we have already traversed it.
      # This is important so that share_params correctly works.
      from_prev = self.sources[0].name
      assert self.network
      subnetwork = self.network.get_calc_step(step)
      prev_layer = subnetwork.get_layer(from_prev)
      assert prev_layer, "%s not found in subnetwork" % from_prev
      if n_out is not None:
        assert n_out == prev_layer.attrs["n_out"]
      self.set_attr("n_out", prev_layer.attrs["n_out"])
      self.output = prev_layer.output


class SubnetworkLayer(_NoOpLayer):
  layer_class = "subnetwork"
  recurrent = True  # we don't know. depends on the subnetwork.

  def __init__(self, n_out, subnetwork, load, data_map=None, trainable=False, **kwargs):
    """
    :param int n_out: output dimension of output layer
    :param dict[str,dict] network: subnetwork as dict (JSON content)
    :param list[str] data_map: maps the sources (from) of the layer to data input.
      the list should be as long as the sources.
      default is ["data"], i.e. it expects one source and maps it as data in the subnetwork.
    :param str load: load string. filename but can have placeholders via str.format. Or "<random>" for no load.
    :param bool trainable: if we take over all params from the subnetwork
    """
    super(SubnetworkLayer, self).__init__(**kwargs)
    self.set_attr("n_out", n_out)
    if isinstance(subnetwork, (str, unicode)):
      subnetwork = json.loads(subnetwork)
    self.set_attr("subnetwork", subnetwork)
    self.set_attr("load", load)
    if isinstance(data_map, (str, unicode)):
      data_map = json.loads(data_map)
    if data_map:
      self.set_attr("data_map", data_map)
    self.set_attr("trainable", trainable)
    if not data_map:
      data_map = ["data"]
    assert isinstance(data_map, list)
    assert len(data_map) == len(self.sources)
    sub_n_out = {"classes": [n_out, 1 if self.attrs['sparse'] else 2]}
    data_map_d = {}
    data_map_di = {"classes": self.index}
    for k, s in zip(data_map, self.sources):
      sub_n_out[k] = [s.attrs["n_out"], s.output.ndim - 1]
      data_map_d[k] = s.output
      data_map_di[k] = s.index
    print >>log.v2, "New subnetwork", self.name, "with data", {k: s.name for (k, s) in zip(data_map, self.sources)}, sub_n_out
    self.subnetwork = self.network.new_subnetwork(
      json_content=subnetwork, n_out=sub_n_out, data_map=data_map_d, data_map_i=data_map_di)
    if trainable:
      self.params.update(self.subnetwork.get_params_shared_flat_dict())
    if load == "<random>":
      print >>log.v2, "subnetwork with random initialization"
    else:
      from Config import get_global_config
      config = get_global_config()  # this is a bit hacky but works fine in all my cases...
      model_filename = load % {"self": self,
                               "global_config_load": config.value("load", None),
                               "global_config_epoch": config.int("epoch", 0)}
      print >>log.v2, "loading subnetwork weights from", model_filename
      import h5py
      model_hdf = h5py.File(model_filename, "r")
      self.subnetwork.load_hdf(model_hdf)
      print >>log.v2, "done loading subnetwork weights for", self.name
    self.output = self.subnetwork.output["output"].output


class ChunkingSublayer(_NoOpLayer):
  layer_class = "chunking_sublayer"
  recurrent = True  # we don't know

  def __init__(self, n_out, sublayer,
               chunk_size, chunk_step,
               chunk_distribution="uniform",
               add_left_context=0,
               trainable=False,
               **kwargs):
    super(ChunkingSublayer, self).__init__(**kwargs)
    self.set_attr('n_out', n_out)
    self.set_attr('chunk_size', chunk_size)
    self.set_attr('chunk_step', chunk_step)
    if isinstance(sublayer, (str, unicode)):
      sublayer = json.loads(sublayer)
    self.set_attr('sublayer', sublayer.copy())
    self.set_attr('chunk_distribution', chunk_distribution)
    self.set_attr('add_left_context', add_left_context)
    self.set_attr('trainable', trainable)

    sub_n_out = sublayer.pop("n_out", None)
    if sub_n_out: assert sub_n_out == n_out
    if trainable:
      sublayer["train_flag"] = self.train_flag
      sublayer["mask"] = self.attrs.get("mask", "none")
      sublayer["dropout"] = self.attrs.get("dropout", 0.0)

    assert len(self.sources) == 1
    source = self.sources[0].output
    n_in = self.sources[0].attrs["n_out"]
    index = self.sources[0].index
    assert source.ndim == 3  # not complicated to support others, just not implemented
    t_last_start = T.maximum(source.shape[0] - chunk_size, 1)
    t_range = T.arange(t_last_start, step=chunk_step)

    from NetworkBaseLayer import SourceLayer
    from NetworkLayer import get_layer_class
    def make_sublayer(source, index, name):
      layer_opts = sublayer.copy()
      cl = layer_opts.pop("class")
      layer_class = get_layer_class(cl)
      source_layer = SourceLayer(name="%s_source" % name, n_out=n_in, x_out=source, index=index)
      layer = layer_class(sources=[source_layer], index=index, name=name, n_out=n_out, network=self.network, **layer_opts)
      self.sublayer = layer
      return layer
    self.sublayer = None

    output = T.zeros((source.shape[0], source.shape[1], n_out), dtype=source.dtype)
    output_index_sum = T.zeros([source.shape[0], source.shape[1]], dtype="float32")
    def step(t_start, output, output_index_sum, source, index):
      t_end = T.minimum(t_start + chunk_size, source.shape[0])
      if add_left_context > 0:
        t_start_c = T.maximum(t_start - add_left_context, 0)
      else:
        t_start_c = t_start
      chunk = source[t_start_c:t_end]
      chunk_index = index[t_start_c:t_end]
      layer = make_sublayer(source=chunk, index=chunk_index, name="%s_sublayer" % self.name)
      l_output = layer.output
      l_index_f32 = T.cast(layer.index, dtype="float32")
      if add_left_context > 0:
        l_output = l_output[t_start - t_start_c:]
        l_index_f32 = l_index_f32[t_start - t_start_c:]
      if chunk_distribution == "uniform": pass  # just leave it as it is
      elif chunk_distribution == "triangle":
        ts = T.arange(1, t_end - t_start + 1)
        ts_rev = ts[::-1]
        tri = T.cast(T.minimum(ts, ts_rev), dtype="float32").dimshuffle(0, 'x')  # time,batch
        l_index_f32 = l_index_f32 * tri
      else:
        assert False, "unknown chunk distribution %r" % chunk_distribution
      assert l_index_f32.ndim == 2
      output = T.inc_subtensor(output[t_start:t_end], l_output * l_index_f32.dimshuffle(0, 1, 'x'))
      output_index_sum = T.inc_subtensor(output_index_sum[t_start:t_end], l_index_f32)
      return [output, output_index_sum]

    (output, output_index_sum), _ = theano.reduce(
      step, sequences=[t_range],
      non_sequences=[source, index],
      outputs_info=[output, output_index_sum])
    self.scan_output = output
    self.scan_output_index_sum = output_index_sum
    self.index = T.gt(output_index_sum, 0)
    output_index_sum = T.maximum(output_index_sum, numpy.float32(1.0))
    assert output.ndim == 3
    assert output_index_sum.ndim == 2
    self.output = output / output_index_sum.dimshuffle(0, 1, 'x')  # renormalize
    assert self.sublayer
    if trainable:
      self.params.update({"sublayer." + name: param for (name, param) in self.sublayer.params.items()})


class ConstantLayer(_NoOpLayer):
  layer_class = "constant"

  def __init__(self, value, n_out, dtype="float32", **kwargs):
    super(ConstantLayer, self).__init__(**kwargs)
    self.set_attr("value", value)
    self.set_attr("dtype", dtype)
    self.set_attr("n_out", n_out)
    value = T.constant(numpy.array(value), dtype=dtype)
    if value.ndim == 0:
      value = value.dimshuffle('x', 'x', 'x')
    elif value.ndim == 1:
      value = value.dimshuffle('x', 'x', 0)
    else:
      raise Exception("ndim %i not supported" % value.ndim)
    assert value.ndim == 3
    source = self.sources[0]
    shape = [source.output.shape[0], source.output.shape[1], n_out]
    value += T.zeros(shape, dtype=dtype)  # so we have the same shape as the source output
    self.make_output(value)


class AddZeroRowsLayer(_NoOpLayer):
  layer_class = "add_zero_rows"

  def __init__(self, row_index, number=1, **kwargs):
    super(AddZeroRowsLayer, self).__init__(**kwargs)
    z, n_out = concat_sources(self.sources, unsparse=True)
    assert 0 <= row_index <= n_out
    n_out += number
    self.set_attr("n_out", n_out)
    self.set_attr("row_index", row_index)
    self.set_attr("number", number)
    self.output = T.concatenate(
      [z[:, :, :row_index],
       T.zeros((z.shape[0], z.shape[1], number), dtype=z.dtype),
       z[:, :, row_index:]],
      axis=2)


class RemoveRowsLayer(_NoOpLayer):
  layer_class = "remove_rows"

  def __init__(self, row_index, number=1, **kwargs):
    super(RemoveRowsLayer, self).__init__(**kwargs)
    z, n_out = concat_sources(self.sources, unsparse=True)
    assert 0 <= row_index + number <= n_out
    n_out -= number
    self.set_attr("n_out", n_out)
    self.set_attr("row_index", row_index)
    self.set_attr("number", number)
    self.output = T.concatenate(
      [z[:, :, :row_index],
       z[:, :, row_index + number:]],
      axis=2)


class BinOpLayer(_NoOpLayer):
  layer_class = "bin_op"

  def __init__(self, op=None, n_out=None, **kwargs):
    """
    :type op: str
    """
    super(BinOpLayer, self).__init__(**kwargs)
    assert len(self.sources) == 2
    s1, s2 = self.sources
    assert s1.attrs["n_out"] == s2.attrs["n_out"]
    if n_out is not None:
      assert n_out == s1.attrs["n_out"]
    assert op
    self.set_attr('op', op.encode("utf8"))
    self.set_attr('n_out', s1.attrs["n_out"])
    if ":" in op:
      op, act = op.split(":", 1)
    else:
      act = None
    op_f = self.get_bin_op(op)
    act_f = strtoact_single_joined(act)
    self.make_output(act_f(op_f(s1.output, s2.output)))

  @staticmethod
  def get_bin_op(op):
    """
    :type op: str
    :rtype: theano.Op
    """
    m = {"+": "add", "-": "sub", "*": "mul", "/": "div"}
    if op in m:
      op = m[op]
    # Assume it's in theano.tensor.
    return getattr(T, op)


class GenericCodeLayer(_NoOpLayer):
  layer_class = "generic_code"

  def __init__(self, code, n_out, **kwargs):
    """
    :param str code: generic Python code used for eval(). must return some output
    """
    super(GenericCodeLayer, self).__init__(**kwargs)
    self.set_attr('n_out', n_out)
    code = code.encode("utf8")
    self.set_attr('code', code)
    import TheanoUtil
    output = eval(code, {"self": self, "s": self.sources,
                         "T": T, "theano": theano, "numpy": numpy, "TU": TheanoUtil,
                         "f32": numpy.float32})
    self.make_output(output)


class DualStateLayer(ForwardLayer):
  layer_class = "dual"

  def __init__(self, acts = "relu", acth = "tanh", **kwargs):
    super(DualStateLayer, self).__init__(**kwargs)
    self.set_attr('acts', acts)
    self.set_attr('acth', acth)
    self.activations = [strtoact(acth), strtoact(acts)]
    self.params = {}
    self.W_in = []
    self.act = [self.b,self.b]  # TODO b is not in params anymore?
    for s,m in zip(self.sources,self.masks):
      assert len(s.act) == 2
      for i,a in enumerate(s.act):
        self.W_in.append(self.add_param(self.create_forward_weights(s.attrs['n_out'],
                                                                    self.attrs['n_out'],
                                                                    name="W_in_%s_%s_%d" % (s.name, self.name, i))))
        if s.attrs['sparse']:
          self.act[i] += self.W_in[-1][T.cast(s.act[i], 'int32')].reshape((s.act[i].shape[0],s.act[i].shape[1],s.act[i].shape[2] * self.W_in[-1].shape[1]))
        elif m is None:
          self.act[i] += self.dot(s.act[i], self.W_in[-1])
        else:
          self.act[i] += self.dot(self.mass * m * s.act[i], self.W_in[-1])
    for i in xrange(2):
      self.act[i] = self.activations[i](self.act[i])
    self.make_output(self.act[0])


class StateToAct(ForwardLayer):
  layer_class = "state_to_act"

  def __init__(self, dual=False, **kwargs):
    kwargs['n_out'] = 1
    super(StateToAct, self).__init__(**kwargs)
    self.set_attr("dual", dual)
    self.params = {}
    self.act = [ T.concatenate([s.act[i][-1] for s in self.sources], axis=1).dimshuffle('x',0,1) for i in xrange(len(self.sources[0].act)) ] # 1BD
    self.attrs['n_out'] = sum([s.attrs['n_out'] for s in self.sources])
    if dual and len(self.act) > 1:
      self.make_output(T.tanh(self.act[1]))
      self.act[0] = T.tanh(self.act[1])
    else:
      self.make_output(self.act[0])
    if 'target' in self.attrs:
      self.output = self.output.repeat(self.index.shape[0],axis=0)
    else:
      self.index = T.ones((1, self.index.shape[1]), dtype = 'int8')


class TimeConcatLayer(HiddenLayer):
  layer_class = "time_concat"

  def __init__(self, **kwargs):
    kwargs['n_out'] = kwargs['sources'][0].attrs['n_out']
    super(TimeConcatLayer, self).__init__(**kwargs)
    self.make_output(T.concatenate([x.output for x in self.sources],axis=0))
    self.index = T.concatenate([x.index for x in self.sources],axis=0)


class HDF5DataLayer(Layer):
  recurrent=True
  layer_class = "hdf5"

  def __init__(self, filename, dset, **kwargs):
    kwargs['n_out'] = 1
    super(HDF5DataLayer, self).__init__(**kwargs)
    self.set_attr('filename', filename)
    self.set_attr('dset', dset)
    import h5py
    h5 = h5py.File(filename, "r")
    data = h5[dset][...]
    self.z = theano.shared(value=data.astype('float32'), borrow=True, name=self.name)
    self.make_output(self.z) # QD
    self.index = T.ones((1, self.index.shape[1]), dtype = 'int8')
    h5.close()


class CentroidLayer2(ForwardLayer):
  recurrent=True
  layer_class="centroid2"

  def __init__(self, centroids, output_scores=False, **kwargs):
    assert centroids
    kwargs['n_out'] = centroids.z.get_value().shape[1]
    super(CentroidLayer2, self).__init__(**kwargs)
    self.set_attr('centroids', centroids.name)
    self.set_attr('output_scores', output_scores)
    self.z = self.output
    diff = T.sqr(self.z.dimshuffle(0,1,'x', 2).repeat(centroids.z.get_value().shape[0], axis=2) - centroids.z.dimshuffle('x','x',0,1).repeat(self.z.shape[0],axis=0).repeat(self.z.shape[1],axis=1)) # TBQD
    if output_scores:
      self.make_output(T.cast(T.argmin(T.sqrt(T.sum(diff, axis=3)),axis=2,keepdims=True),'float32'))
    else:
      self.make_output(centroids.z[T.argmin(T.sqrt(T.sum(diff, axis=3)), axis=2)])

    if 'dual' in centroids.attrs:
      self.act = [ T.tanh(self.output), self.output ]
    else:
      self.act = [ self.output, self.output ]


class CentroidLayer(ForwardLayer):
  recurrent=True
  layer_class="centroid"

  def __init__(self, centroids, output_scores=False, entropy_weight=1.0, **kwargs):
    assert centroids
    kwargs['n_out'] = centroids.z.get_value().shape[1]
    super(CentroidLayer, self).__init__(**kwargs)
    self.set_attr('centroids', centroids.name)
    self.set_attr('output_scores', output_scores)
    self.set_attr('entropy_weight', entropy_weight)
    W_att_ce = self.add_param(self.create_forward_weights(centroids.z.get_value().shape[1], 1), name = "W_att_ce_%s" % self.name)
    W_att_in = self.add_param(self.create_forward_weights(self.attrs['n_out'], 1), name = "W_att_in_%s" % self.name)

    zc = centroids.z.dimshuffle('x','x',0,1).repeat(self.z.shape[0],axis=0).repeat(self.z.shape[1],axis=1) # TBQD
    ze = T.exp(T.dot(zc, W_att_ce) + T.dot(self.z, W_att_in).dimshuffle(0,1,'x',2).repeat(centroids.z.get_value().shape[0],axis=2)) # TBQ1
    att = ze / T.sum(ze, axis=2, keepdims=True) # TBQ1
    if output_scores:
      self.make_output(att.flatten(ndim=3))
    else:
      self.make_output(T.sum(att.repeat(self.attrs['n_out'],axis=3) * zc,axis=2)) # TBD

    self.constraints += entropy_weight * -T.sum(att * T.log(att))

    if 'dual' in centroids.attrs:
      self.act = [ T.tanh(self.output), self.output ]
    else:
      self.act = [ self.output, self.output ]


class CentroidEyeLayer(ForwardLayer):
  recurrent=True
  layer_class="eye"

  def __init__(self, n_clusters, output_scores=False, entropy_weight=0.0, **kwargs):
    centroids = T.eye(n_clusters)
    kwargs['n_out'] = n_clusters
    super(CentroidEyeLayer, self).__init__(**kwargs)
    self.set_attr('n_clusters', n_clusters)
    self.set_attr('output_scores', output_scores)
    self.set_attr('entropy_weight', entropy_weight)
    W_att_ce = self.add_param(self.create_forward_weights(n_clusters, 1), name = "W_att_ce_%s" % self.name)
    W_att_in = self.add_param(self.create_forward_weights(self.attrs['n_out'], 1), name = "W_att_in_%s" % self.name)

    zc = centroids.dimshuffle('x','x',0,1).repeat(self.z.shape[0],axis=0).repeat(self.z.shape[1],axis=1) # TBQD
    ze = T.exp(T.dot(zc, W_att_ce) + T.dot(self.z, W_att_in).dimshuffle(0,1,'x',2).repeat(n_clusters,axis=2)) # TBQ1
    att = ze / T.sum(ze, axis=2, keepdims=True) # TBQ1
    if output_scores:
      self.make_output(att.flatten(ndim=3))
    else:
      self.make_output(T.sum(att.repeat(self.attrs['n_out'],axis=3) * zc,axis=2)) # TBD
      #self.make_output(centroids[T.argmax(att.reshape((att.shape[0],att.shape[1],att.shape[2])), axis=2)])

    self.constraints += entropy_weight * -T.sum(att * T.log(att))
    self.act = [ T.tanh(self.output), self.output ]


class ProtoLayer(ForwardLayer):
  recurrent=True
  layer_class="proto"

  def __init__(self, train_proto=True, output_scores=False, **kwargs):
    super(ProtoLayer, self).__init__(**kwargs)
    W_proto = self.create_random_uniform_weights(self.attrs['n_out'], self.attrs['n_out'])
    if train_proto:
      self.add_param(W_proto, name = "W_proto_%s" % self.name)
    if output_scores:
      self.make_output(T.cast(T.argmax(self.z,axis=-1,keepdims=True),'float32'))
    else:
      self.make_output(W_proto[T.argmax(self.z,axis=-1)])
    self.act = [ T.tanh(self.output), self.output ]


class BaseInterpolationLayer(ForwardLayer): # takes a base defined over T and input defined over T' and outputs a T' vector built over an input dependent linear combination of the base elements
  layer_class = "base"

  def __init__(self, base=None, method="softmax", output_weights = False, **kwargs):
    assert base, "missing base in " + kwargs['name']
    kwargs['n_out'] = 1
    super(BaseInterpolationLayer, self).__init__(**kwargs)
    self.set_attr('base', ",".join([b.name for b in base]))
    self.set_attr('method', method)
    self.W_base = [ self.add_param(self.create_forward_weights(bs.attrs['n_out'], 1, name='W_base_%s_%s' % (bs.attrs['n_out'], self.name)), name='W_base_%s_%s' % (bs.attrs['n_out'], self.name)) for bs in base ]
    self.base = T.concatenate([b.output for b in base], axis=2) # TBD
    # self.z : T'
    bz = 0 # : T
    for x,W in zip(base, self.W_base):
      bz += T.dot(x.output,W) # TB1
    z = bz.reshape((bz.shape[0],bz.shape[1])).dimshuffle('x',1,0) + self.z.reshape((self.z.shape[0],self.z.shape[1])).dimshuffle(0,1,'x') # T'BT
    h = z.reshape((z.shape[0] * z.shape[1], z.shape[2])) # (T'xB)T
    if method == 'softmax':
      h_e = T.exp(h).dimshuffle(1,0)
      w = (h_e / T.sum(h_e, axis=0)).dimshuffle(1,0).reshape(z.shape).dimshuffle(2,1,0,'x').repeat(self.base.shape[2], axis=3) # TBT'D
      #w = T.nnet.softmax(h).reshape(z.shape).dimshuffle(2,1,0,'x').repeat(self.base.shape[2], axis=3) # TBT'D
    else:
      assert False, "invalid method %s in %s" % (method, self.name)

    self.set_attr('n_out', sum([b.attrs['n_out'] for b in base]))
    if output_weights:
      self.make_output((h_e / T.sum(h_e, axis=0, keepdims=True)).dimshuffle(1,0).reshape((self.base.shape[0],z.shape[1],z.shape[0])).dimshuffle(2,1,0))
    else:
      self.make_output(T.sum(self.base.dimshuffle(0,1,'x',2).repeat(z.shape[0], axis=2) * w, axis=0, keepdims=False).dimshuffle(1,0,2)) # T'BD


class ChunkingLayer(ForwardLayer): # Time axis reduction like in pLSTM described in http://arxiv.org/pdf/1508.01211v1.pdf
  layer_class = "chunking"

  def __init__(self, chunk_size=1, method = 'concat', **kwargs):
    assert chunk_size >= 1
    kwargs['n_out'] = sum([s.attrs['n_out'] for s in kwargs['sources']]) * chunk_size
    super(ChunkingLayer, self).__init__(**kwargs)
    self.set_attr('chunk_size', chunk_size)
    z = T.concatenate([s.output for s in self.sources], axis=2) # TBD
    residual = z.shape[0] % chunk_size
    padding = T.neq(residual,0) * (chunk_size - residual)

    calloc = T.alloc(numpy.cast[theano.config.floatX](0), z.shape[0] + padding, z.shape[1], z.shape[2])
    container = T.set_subtensor(
      calloc[:z.shape[0]],
      z).dimshuffle('x',0,1,2).reshape((chunk_size,calloc.shape[0] / chunk_size,calloc.shape[1],calloc.shape[2])) # CTBD
    z = T.concatenate([z,T.zeros((padding,z.shape[1],z.shape[2]), 'float32')], axis=0).dimshuffle('x',0,1,2).reshape((chunk_size,(z.shape[0] + padding) / chunk_size,z.shape[1],z.shape[2]))
    #ialloc = T.alloc(numpy.cast['int32'](1), z.shape[1], self.index.shape[1])
    self.index = T.set_subtensor(T.ones((z.shape[1]*z.shape[0],z.shape[2]),'int8')[:self.index.shape[0]],self.index)[::chunk_size]

    if method == 'concat':
      output = z.dimshuffle(1,2,3,0).reshape((z.shape[1], z.shape[2], z.shape[3] * chunk_size))
    elif method == 'average':
      output = z.mean(axis=0)
    self.make_output(output)


class LengthLayer(HiddenLayer):
  layer_class = "length"
  def __init__(self, min_len=0.0, max_len=1.0, use_real=1.0, err='ce', oracle=False, **kwargs):
    kwargs['n_out'] = 1
    super(LengthLayer, self).__init__(**kwargs)
    self.set_attr('min_len',min_len)
    self.set_attr('max_len',max_len)
    pcx = T.exp(self.get_linear_forward_output()[:,:,0]) * T.cast(self.index, 'float32')
    pcx = pcx / T.sum(pcx,axis=0,keepdims=True)
    hyp = T.sum(T.arange(pcx.shape[0],dtype='float32').dimshuffle(0,'x').repeat(pcx.shape[1],axis=1) * pcx, axis=0) + numpy.float32(1)
    real = T.sum(T.cast(self.sources[0].target_index,'float32'), axis=0)
    if err == 'ce':
      pos = -T.log(T.clip(pcx[T.cast(real,'int32')-1], T.constant(1e-20,'float32'), T.constant(1.0,'float32')))
      neg = -T.log(T.clip(numpy.float32(1.) - pcx, T.constant(1e-20,'float32'), T.constant(1.0,'float32')))
      self.cost_len = T.maximum(T.sum(pos) + T.sum(neg) - T.sum(neg[T.cast(real,'int32')-1]), 0.0) * T.sum(T.cast(self.sources[0].target_index.shape[1],'float32')) / T.sum(T.cast(self.sources[0].index.shape[1],'float32'))
    elif err == 'l2':
      self.cost_len = T.sum(self.sources[0].target_index) * T.mean((real - hyp)**2)
    elif err == 'exp':
      self.cost_len = T.sum(self.sources[0].target_index) * T.mean(T.switch(T.lt(real + numpy.float32(1.),hyp),
                                                                   T.sqrt(hyp-real), (real - hyp)**2))
    else:
      assert False, "invalid error: %s" % err

    if oracle or self.train_flag:
      self.length = T.cast(numpy.float32(use_real) * real + numpy.float32(1. - use_real) * hyp,'int32')
    else:
      self.length = T.cast(hyp,'int32')

    idx, _ = theano.map(lambda l_t,m_t:T.concatenate([T.ones((l_t, ), 'int8'), T.zeros((m_t - l_t, ), 'int8')]),
                        sequences = [self.length], non_sequences=[T.max(self.length) + 1])
    self.index = idx.dimshuffle(1,0)[:-1]
    self.output = self.b

  def cost(self):
    return self.cost_len, None

  def cost_scale(self):
    return T.constant(self.attrs.get("cost_scale", 1.0), dtype="float32")


class TruncationLayer(Layer):
  layer_class = "trunc"

  def __init__(self, n_trunc, **kwargs):
    kwargs['n_out'] = sum([s.attrs['n_out'] for s in kwargs['sources']])
    super(TruncationLayer, self).__init__(**kwargs)
    self.set_attr('from', ",".join([s.name for s in self.sources]))
    self.set_attr('n_trunc', n_trunc)
    n_trunc = T.switch(T.gt(n_trunc, self.index.shape[0]), self.index.shape[0], n_trunc)
    z = T.concatenate([s.output for s in self.sources], axis=2)
    self.index = self.index[:n_trunc]
    self.make_output(z[:n_trunc])
    #self.make_output(z)


class CorruptionLayer(ForwardLayer): # x = x + noise
  layer_class = "corruption"
  from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
  rng = RandomStreams(hash(layer_class) % 4294967295)

  def __init__(self, noise='gaussian', p=0.0, **kwargs):
    kwargs['n_out'] = sum([s.attrs['n_out'] for s in kwargs['sources']])
    super(CorruptionLayer, self).__init__(**kwargs)
    self.set_attr('noise', noise)
    self.set_attr('p', p)

    z = T.concatenate([s.output for s in self.sources], axis=2)
    if noise == 'gaussian':
      z = self.rng.normal(size=z.shape,avg=0,std=p,dtype='float32') + (z - T.mean(z, axis=(0,1), keepdims=True)) / T.std(z, axis=(0,1), keepdims=True)
    self.make_output(z)

class InputBase(Layer):
  layer_class = "input_base"

  def __init__(self, **kwargs):
    kwargs['n_out'] = 1
    super(InputBase, self).__init__(**kwargs)
    assert len(self.sources) == 1
    self.set_attr('from', ",".join([s.name for s in self.sources]))
    self.make_output(self.sources[0].W_in[0].dimshuffle(0,'x',1).repeat(self.index.shape[1],axis=1))
    self.set_attr('n_out', self.sources[0].W_in[0].get_value().shape[1])

class ConvPoolLayer(ForwardLayer):
  layer_class = "convpool"

  def __init__(self, dx, dy, fx, fy, **kwargs):
    kwargs['n_out'] = fx * fy
    super(ConvPoolLayer, self).__init__(**kwargs)
    self.set_attr('dx', dx) # receptive fields
    self.set_attr('dy', dy)
    self.set_attr('fx', fx) # receptive fields
    self.set_attr('fy', fy)

    # instantiate 4D tensor for input
    n_in = numpy.sum([s.output for s in self.sources])
    assert n_in == dx * dy
    x_in  = T.concatenate([s.output for s in self.sources], axis = -1).dimshuffle(0,1,2,'x').reshape(self.sources[0].shape[0], self.sources[0].shape[1],dx, dy)
    range = 1.0 / numpy.sqrt(dx*dy)
    self.W = self.add_param(theano.shared( numpy.asarray(self.rng.uniform(low=-range,high=range,size=(2,1,fx,fy)), dtype = theano.config.floatX), name = "W_%s" % self.name), name = "W_%s" % self.name)
    conv_out = conv.conv2d(x_in, self.W)

    # initialize shared variable for weights.
    w_shp = (2, 3, 9, 9)
    w_bound = numpy.sqrt(3 * 9 * 9)
    W = theano.shared( numpy.asarray(
                rng.uniform(
                    low=-1.0 / w_bound,
                    high=1.0 / w_bound,
                    size=w_shp),
                dtype=input.dtype), name ='W')

    # initialize shared variable for bias (1D tensor) with random values
    # IMPORTANT: biases are usually initialized to zero. However in this
    # particular application, we simply apply the convolutional layer to
    # an image without learning the parameters. We therefore initialize
    # them to random values to "simulate" learning.
    b_shp = (2,)
    b = theano.shared(numpy.asarray(
                rng.uniform(low=-.5, high=.5, size=b_shp),
                dtype=input.dtype), name ='b')

    # build symbolic expression that computes the convolution of input with filters in w
    conv_out = conv.conv2d(input, W)

    # build symbolic expression to add bias and apply activation function, i.e. produce neural net layer output
    # A few words on ``dimshuffle`` :
    #   ``dimshuffle`` is a powerful tool in reshaping a tensor;
    #   what it allows you to do is to shuffle dimension around
    #   but also to insert new ones along which the tensor will be
    #   broadcastable;
    #   dimshuffle('x', 2, 'x', 0, 1)
    #   This will work on 3d tensors with no broadcastable
    #   dimensions. The first dimension will be broadcastable,
    #   then we will have the third dimension of the input tensor as
    #   the second of the resulting tensor, etc. If the tensor has
    #   shape (20, 30, 40), the resulting tensor will have dimensions
    #   (1, 40, 1, 20, 30). (AxBxC tensor is mapped to 1xCx1xAxB tensor)
    #   More examples:
    #    dimshuffle('x') -> make a 0d (scalar) into a 1d vector
    #    dimshuffle(0, 1) -> identity
    #    dimshuffle(1, 0) -> inverts the first and second dimensions
    #    dimshuffle('x', 0) -> make a row out of a 1d vector (N to 1xN)
    #    dimshuffle(0, 'x') -> make a column out of a 1d vector (N to Nx1)
    #    dimshuffle(2, 0, 1) -> AxBxC to CxAxB
    #    dimshuffle(0, 'x', 1) -> AxB to Ax1xB
    #    dimshuffle(1, 'x', 0) -> AxB to Bx1xA
    output = T.nnet.sigmoid(conv_out + b.dimshuffle('x', 0, 'x', 'x'))

    # create theano function to compute filtered images
    f = theano.function([input], output)


class LossLayer(Layer):
  layer_class = "loss"

  def __init__(self, loss, copy_input=None, **kwargs):
    """
    :param theano.Variable index: index for batches
    :param str loss: e.g. 'ce'
    """
    super(LossLayer, self).__init__(**kwargs)
    y = self.y_in
    if copy_input:
      self.set_attr("copy_input", copy_input.name)
    if not copy_input:
      self.z = self.b
      self.W_in = [self.add_param(self.create_forward_weights(source.attrs['n_out'], self.attrs['n_out'],
                                                              name="W_in_%s_%s" % (source.name, self.name)))
                   for source in self.sources]

      assert len(self.sources) == len(self.masks) == len(self.W_in)
      assert len(self.sources) > 0
      for source, m, W in zip(self.sources, self.masks, self.W_in):
        if source.attrs['sparse']:
          self.z += W[T.cast(source.output[:,:,0], 'int32')]
        elif m is None:
          self.z += self.dot(source.output, W)
        else:
          self.z += self.dot(self.mass * m * source.output, W)
    else:
      self.z = copy_input.output
    self.set_attr('from', ",".join([s.name for s in self.sources]))
    if self.y.dtype.startswith('int'):
      i = (self.index.flatten() > 0).nonzero()
    elif self.y.dtype.startswith('float'):
      i = (self.index.flatten() > 0).nonzero()
    self.j = ((T.constant(1.0) - self.index.flatten()) > 0).nonzero()
    loss = loss.encode("utf8")
    self.attrs['loss'] = self.loss
    n_reps = T.switch(T.eq(self.z.shape[0], 1), self.index.shape[0], 1)
    output = self.output.repeat(n_reps,axis=0)
    y_m = output.reshape((output.shape[0]*output.shape[1],output.shape[2]))
    self.known_grads = None
    if loss == 'ce':
      if self.y.type == T.ivector().type:
        nll, pcx = T.nnet.crossentropy_softmax_1hot(x=y_m[i], y_idx=y[i])
      else:
        pcx = T.nnet.softmax(y_m[i])
        nll = -T.dot(T.log(T.clip(pcx, 1.e-38, 1.e20)), y[i].T)
      self.constraints += T.sum(nll)
      self.make_output(pcx.reshape(output.shape))
    elif loss == 'entropy':
      h_e = T.exp(self.y_m) #(TB)
      pcx = T.clip((h_e / T.sum(h_e, axis=1, keepdims=True)).reshape((self.index.shape[0],self.index.shape[1],self.attrs['n_out'])), 1.e-6, 1.e6) # TBD
      ee = self.index * -T.sum(pcx * T.log(pcx)) # TB
      nll, pcx = T.nnet.crossentropy_softmax_1hot(x=self.y_m, y_idx=self.y) # TB
      ce = nll.reshape(self.index.shape) * self.index # TB
      y = self.y.reshape(self.index.shape) * self.index # TB
      f = T.any(T.gt(y,0), axis=0) # B
      self.constraints += T.sum(f * T.sum(ce, axis=0) + (1-f) * T.sum(ee, axis=0))
      self.make_output(pcx.reshape(output.shape))
    elif loss == 'priori':
      pcx = T.nnet.softmax(y_m)[i, y[i]]
      pcx = T.clip(pcx, 1.e-38, 1.e20)  # For pcx near zero, the gradient will likely explode.
      self.constraints += -T.sum(T.log(pcx))
      self.make_output(pcx.reshape(output.shape))
    elif loss == 'sse':
      if self.y.dtype.startswith('int'):
        y_f = T.cast(T.reshape(y, (y.shape[0] * y.shape[1]), ndim=1), 'int32')
        y_oh = T.eq(T.shape_padleft(T.arange(self.attrs['n_out']), y_f.ndim), T.shape_padright(y_f, 1))
        self.constraints += T.mean(T.sqr(y_m[i] - y_oh[i]))
      else:
        self.constraints += T.sum(T.sqr(y_m[i] - y.reshape(y_m.shape)[i]))
      self.make_output(y_m[i].reshape(output.shape))
    else:
      raise NotImplementedError()

    if y.dtype.startswith('int'):
      if y.type == T.ivector().type:
        self.error = T.sum(T.neq(T.argmax(y_m[i], axis=-1), y[i]))
      else:
        self.error = T.sum(T.neq(T.argmax(y_m[i], axis=-1), T.argmax(y[i], axis = -1)))
    elif y.dtype.startswith('float'):
      self.error = T.sum(T.sqr(y_m[i] - y.reshape(y_m.shape)[i]))
    else:
      raise NotImplementedError()


class ErrorsLayer(_NoOpLayer):
  layer_class = "errors"

  def __init__(self, target, **kwargs):
    super(ErrorsLayer, self).__init__(**kwargs)
    self.set_attr("target", target)
    assert target in self.network.y
    self.y = self.network.y[target]
    assert self.y.ndim == 2
    n_out = self.network.n_out[target][0]
    self.set_attr("n_out", n_out)
    self.set_attr("sparse", True)
    self.z, z_dim = concat_sources(self.sources, unsparse=True)
    assert z_dim == n_out
    self.output = T.neq(T.argmax(self.z, axis=2), self.y)

  def errors(self):
    """
    :rtype: theano.Variable
    """
    return T.sum(self.output)


############################################ START HERE #####################################################
class ConvLayer(_NoOpLayer):
  layer_class = "conv_layer"

  """
    This is class for Convolution Neural Networks
    Get the reference from deeplearning.net/tutorial/lenet.html
  """

  def __init__(self, dimension_row, dimension_col, n_features, filter_row, filter_col, stack_size=1,
               pool_size=(2, 2), border_mode='valid', ignore_border=True, **kwargs):
    """
    :param dimension_row: integer
        the number of row(s) from the input
    :param dimension_col: integer
        the number of column(s) from the input
    :param n_features: integer
        the number of feature map(s) / filter(S) that will be used for the filter shape
    :param filter_row: integer
        the number of row(s) from the filter shape
    :param filter_col: integer
        the number of column(s) from the filter shape
    :param stack_size: integer
        the number of color channel (default is Gray scale) for the first input layer and
        the number of feature mapss/filters from the previous layer for the convolution layer
        (default value is 1)
    :param pool_size: tuple of length 2
        Factor by which to downscale (vertical, horizontal)
        (default value is (2, 2))
    :param border_mode: string
        'valid'-- only apply filter to complete patches of the image. Generates
                  output of shape: (image_shape - filter_shape + 1)
        'full' -- zero-pads image to multiple of filter shape to generate output
                  of shape: (image_shape + filter_shape - 1)
        (default value is 'valid')
    :param ignore_border: boolean
        True  -- (5, 5) input with pool_size = (2, 2), will generate a (2, 2) output.
        False -- (5, 5) input with pool_size = (2, 2), will generate a (3, 3) output.
    """

    # number of output dimension validation based on the border_mode
    if border_mode == 'valid':
      conv_n_out = (dimension_row - filter_row + 1) * (dimension_col - filter_col + 1)
    elif border_mode == 'full':
      conv_n_out = (dimension_row + filter_row - 1) * (dimension_col + filter_col - 1)
    else:
      assert False, 'invalid border_mode %r' % border_mode

    n_out = conv_n_out * n_features / (pool_size[0] * pool_size[1])
    super(ConvLayer, self).__init__(**kwargs)

    # set all attributes of this class
    self.set_attr('n_out', n_out)  # number of output dimension
    self.set_attr('dimension_row', dimension_row)
    self.set_attr('dimension_col', dimension_col)
    self.set_attr('n_features', n_features)
    self.set_attr('filter_row', filter_row)
    self.set_attr('filter_col', filter_col)
    self.set_attr('stack_size', stack_size)
    self.set_attr('pool_size', pool_size)
    self.set_attr('border_mode', border_mode)
    self.set_attr('ignore_border', ignore_border)

    n_in = sum([s.attrs['n_out'] for s in self.sources])
    assert n_in == dimension_row * dimension_col * stack_size

    # our CRNN input is 3D tensor that consists of (time, batch, dim)
    # however, the convolution function only accept 4D tensor which is (batch size, stack size, nb row, nb col)
    # therefore, we should convert our input into 4D tensor
    input = T.concatenate([s.output for s in self.sources], axis=-1)  # (time, batch, input-dim = row * col * stack_size)
    input.name = 'conv_layer_input_concat'
    time = input.shape[0]
    batch = input.shape[1]
    input2 = input.reshape((time * batch, dimension_row, dimension_col, stack_size))  # (time * batch, row, col, stack_size)
    self.input = input2.dimshuffle(0, 3, 1, 2)  # (batch, stack_size, row, col)
    self.input.name = 'conv_layer_input_final'

    # filter shape is tuple/list of length 4 which is (nb filters, stack size, filter row, filter col)
    self.filter_shape = (n_features, stack_size, filter_row, filter_col)

    # weight parameter
    self.W = self.add_param(self._create_weights(filter_shape=self.filter_shape, pool_size=pool_size))
    # bias parameter
    self.b = self.add_param(self._create_bias(n_features=n_features))

    # convolution function
    self.conv_out = conv.conv2d(
      input=self.input,
      filters=self.W,
      filter_shape=self.filter_shape,
      border_mode=border_mode
    )
    self.conv_out.name = 'conv_layer_conv_out'

    # max pooling function
    self.pooled_out = downsample.max_pool_2d(
      input=self.conv_out,
      ds=pool_size,
      ignore_border=ignore_border
    )
    self.pooled_out.name = 'conv_layer_pooled_out'

    # calculate the convolution output which returns (batch, nb filters, nb row, nb col)
    output = T.tanh(self.pooled_out + self.b.dimshuffle('x', 0, 'x', 'x'))  # (time*batch, filter, out-row, out-col)
    output.name = 'conv_layer_output_plus_bias'

    # our CRNN only accept 3D tensor (time, batch, dim)
    # so, we have to convert the output back to 3D tensor
    output2 = output.dimshuffle(0, 2, 3, 1)  # (time*batch, out-row, out-col, filter)
    self.output = output2.reshape((time, batch, output2.shape[1] * output2.shape[2] * output2.shape[3]))  # (time, batch, out-dim)
    self.make_output(self.output)

  # function for calculating the weight parameter of this class
  def _create_weights(self, filter_shape, pool_size):
    rng = numpy.random.RandomState(23455)
    fan_in = numpy.prod(filter_shape[1:])  # stack_size * filter_row * filter_col
    fan_out = (filter_shape[0] * numpy.prod(filter_shape[2:]) / numpy.prod(pool_size))  # (n_features * (filter_row * filter_col)) / (pool_size[0] * pool_size[1])

    W_bound = numpy.sqrt(6. / (fan_in + fan_out))
    return theano.shared(
      numpy.asarray(
        rng.uniform(low=-W_bound, high=W_bound, size=filter_shape),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="W_conv"
    )

  # function for calculating the bias parameter of this class
  def _create_bias(self, n_features):
    return theano.shared(
      numpy.zeros(
        (n_features,),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="b_conv"
    )
############################################# END HERE ######################################################
###########################################TRYING BORDER_MODE = 'SAME'#######################################
class NewConvLayer(_NoOpLayer):
  layer_class = "new_conv_layer"

  """
    This is class for Convolution Neural Networks
    Get the reference from deeplearning.net/tutorial/lenet.html
  """

  def __init__(self, dimension_row, dimension_col, n_features, filter_row, filter_col, stack_size=1,
               pool_size=(2, 2), border_mode='valid', ignore_border=True, **kwargs):
    """

    :param dimension_row: integer
        the number of row(s) from the input

    :param dimension_col: integer
        the number of column(s) from the input

    :param n_features: integer
        the number of feature map(s) / filter(S) that will be used for the filter shape

    :param filter_row: integer
        the number of row(s) from the filter shape

    :param filter_col: integer
        the number of column(s) from the filter shape

    :param stack_size: integer
        the number of color channel (default is Gray scale) for the first input layer and
        the number of feature mapss/filters from the previous layer for the convolution layer
        (default value is 1)

    :param pool_size: tuple of length 2
        Factor by which to downscale (vertical, horizontal)
        (default value is (2, 2))

    :param border_mode: string
        'valid'-- only apply filter to complete patches of the image. Generates
                  output of shape: (image_shape - filter_shape + 1)
        'full' -- zero-pads image to multiple of filter shape to generate output
                  of shape: (image_shape + filter_shape - 1)
        (default value is 'valid')

    :param ignore_border: boolean
        True  -- (5, 5) input with pool_size = (2, 2), will generate a (2, 2) output.
        False -- (5, 5) input with pool_size = (2, 2), will generate a (3, 3) output.

    """

    # number of output dimension validation based on the border_mode
    if border_mode == 'valid':
      conv_n_out = (dimension_row - filter_row + 1) * (dimension_col - filter_col + 1)
    elif border_mode == 'full':
      conv_n_out = (dimension_row + filter_row - 1) * (dimension_col + filter_col - 1)
    elif border_mode == 'same':
      conv_n_out = (dimension_row * dimension_col)
    else:
      assert False, 'invalid border_mode %r' % border_mode

    n_out = conv_n_out * n_features / (pool_size[0] * pool_size[1])
    super(NewConvLayer, self).__init__(**kwargs)

    # set all attributes of this class
    self.set_attr('n_out', n_out)  # number of output dimension
    self.set_attr('dimension_row', dimension_row)
    self.set_attr('dimension_col', dimension_col)
    self.set_attr('n_features', n_features)
    self.set_attr('filter_row', filter_row)
    self.set_attr('filter_col', filter_col)
    self.set_attr('stack_size', stack_size)
    self.set_attr('pool_size', pool_size)
    self.set_attr('border_mode', border_mode)
    self.set_attr('ignore_border', ignore_border)

    n_in = sum([s.attrs['n_out'] for s in self.sources])
    assert n_in == dimension_row * dimension_col * stack_size

    # our CRNN input is 3D tensor that consists of (time, batch, dim)
    # however, the convolution function only accept 4D tensor which is (batch size, stack size, nb row, nb col)
    # therefore, we should convert our input into 4D tensor
    input = T.concatenate([s.output for s in self.sources], axis=-1)  # (time, batch, input-dim = row * col * stack_size)
    input.name = 'conv_layer_input_concat'
    time = input.shape[0]
    batch = input.shape[1]
    input2 = input.reshape((time * batch, dimension_row, dimension_col, stack_size))  # (time * batch, row, col, stack_size)
    self.input = input2.dimshuffle(0, 3, 1, 2)  # (batch, stack_size, row, col)
    self.input.name = 'conv_layer_input_final'

    # filter shape is tuple/list of length 4 which is (nb filters, stack size, filter row, filter col)
    self.filter_shape = (n_features, stack_size, filter_row, filter_col)

    # weight parameter
    self.W = self.add_param(self._create_weights(filter_shape=self.filter_shape, pool_size=pool_size))
    # bias parameter
    self.b = self.add_param(self._create_bias(n_features=n_features))

    # convolution function
    if border_mode == 'same':
      new_filter_size = self.W.shape[2]-1
      self.conv_out = conv.conv2d(
        input=self.input,
        filters=self.W,
        filter_shape=self.filter_shape,
        border_mode='full'
      )[:,:,new_filter_size:dimension_row+new_filter_size,new_filter_size:dimension_col+new_filter_size]
    else:
      self.conv_out = conv.conv2d(
        input=self.input,
        filters=self.W,
        filter_shape=self.filter_shape,
        border_mode=border_mode
      )
    self.conv_out.name = 'conv_layer_conv_out'

    # max pooling function
    self.pooled_out = downsample.max_pool_2d(
      input=self.conv_out,
      ds=pool_size,
      ignore_border=ignore_border
    )
    self.pooled_out.name = 'conv_layer_pooled_out'

    # calculate the convolution output which returns (batch, nb filters, nb row, nb col)
    output = elu(self.pooled_out + self.b.dimshuffle('x', 0, 'x', 'x'))  # (time*batch, filter, out-row, out-col)
    output.name = 'conv_layer_output_plus_bias'

    # our CRNN only accept 3D tensor (time, batch, dim)
    # so, we have to convert the output back to 3D tensor
    output2 = output.dimshuffle(0, 2, 3, 1)  # (time*batch, out-row, out-col, filter)
    self.output = output2.reshape((time, batch, output2.shape[1] * output2.shape[2] * output2.shape[3]))  # (time, batch, out-dim)
    self.make_output(self.output)

  # function for calculating the weight parameter of this class
  def _create_weights(self, filter_shape, pool_size):
    rng = numpy.random.RandomState(23455)
    fan_in = numpy.prod(filter_shape[1:])  # stack_size * filter_row * filter_col
    fan_out = (filter_shape[0] * numpy.prod(filter_shape[2:]) / numpy.prod(pool_size))  # (n_features * (filter_row * filter_col)) / (pool_size[0] * pool_size[1])

    W_bound = numpy.sqrt(6. / (fan_in + fan_out))
    return theano.shared(
      numpy.asarray(
        rng.uniform(low=-W_bound, high=W_bound, size=filter_shape),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="W_conv"
    )

  # function for calculating the bias parameter of this class
  def _create_bias(self, n_features):
    return theano.shared(
      numpy.zeros(
        (n_features,),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="b_conv"
    )
############################################# END HERE ######################################################





################################################# NEW AUTOMATIC CONVOLUTIONAL LAYER #######################################################
class NewConv(_NoOpLayer):
  layer_class = "conv"

  """
    This is class for Convolution Neural Networks
    Get the reference from deeplearning.net/tutorial/lenet.html
  """

  def __init__(self, n_features, filter, d_row=1, pool_size=(2, 2), border_mode='valid', ignore_border=True, **kwargs):

    """

    :param n_features: integer
        the number of feature map(s) / filter(S) that will be used for the filter shape

    :param filter: integer
        the number of row(s) or columns(s) from the filter shape
        this filter is the square, therefore we only need one parameter that represents row and column

    :param d_row: integer
        the number of row(s) from the input
        this has to be filled only for the first convolutional neural network layer
        the remaining layer will used the number of row from the previous layer

    :param pool_size: tuple of length 2
        Factor by which to downscale (vertical, horizontal)
        (default value is (2, 2))

    :param border_mode: string
        'valid'-- only apply filter to complete patches of the image. Generates
                  output of shape: (image_shape - filter_shape + 1)
        'full' -- zero-pads image to multiple of filter shape to generate output
                  of shape: (image_shape + filter_shape - 1)
        'same' -- the size of image will remain the same with the previous layer
        (default value is 'valid')

    :param ignore_border: boolean
        True  -- (5, 5) input with pool_size = (2, 2), will generate a (2, 2) output.
        False -- (5, 5) input with pool_size = (2, 2), will generate a (3, 3) output.

    """

    super(NewConv, self).__init__(**kwargs)

    n_sources = len(self.sources)   # calculate how many input
    is_conv_layer = all(s.layer_class == 'conv' for s in self.sources)  # check whether all inputs are conv layers

    # check whether the input is conv layer
    if is_conv_layer:
      d_row = self.sources[0].attrs['d_row']  # set number of input row from the previous conv layer
      stack_size = self.sources[0].attrs['n_features']  # set stack_size from the number of previous layer feature maps
      dimension = self.sources[0].attrs['n_out']/stack_size   # calculate the input dimension

      # check whether number of inputs are more than 1 for concatenating the inputs
      if n_sources != 1:
        # check the spatial dimension of all inputs
        assert all((s.attrs['n_out']/s.attrs['n_features']) == (self.sources[0].attrs['n_out']/self.sources[0].attrs['n_features']) for s in self.sources), 'Sorry, the spatial dimension of all inputs have to be the same'
        stack_size = sum([s.attrs['n_features'] for s in self.sources])   # set the stack_size from concatenating the input feature maps
    else:   # input is not conv layer
      stack_size = 1  # set stack_size of first conv layer as channel of the image (grayscale image)
      dimension = self.sources[0].attrs['n_out']  # set the dimension of input

      # whether number of inputs are more than 1 for concatenating the inputs
      if n_sources != 1:
        # check the number of layer unit
        assert all(s.attrs['n_out'] == self.sources[0].attrs['n_out'] for s in self.sources), 'Sorry, the units of all inputs have to be the same'
        dimension = sum([s.attrs['n_out'] for s in self.sources])   # set the dimension by concatenating the number of output from input

    # calculating the number of input columns
    d_col = dimension/d_row

    # number of output dimension validation based on the border_mode
    if border_mode == 'valid':
      d_row_new = (d_row - filter + 1)/pool_size[0]
      d_col_new = (d_col - filter + 1)/pool_size[1]
    elif border_mode == 'full':
      d_row_new = (d_row + filter - 1)/pool_size[0]
      d_col_new = (d_col + filter - 1)/pool_size[1]
    elif border_mode == 'same':
      d_row_new = d_row/pool_size[0]
      d_col_new = d_col/pool_size[1]
    else:
      assert False, 'invalid border_mode %r' % border_mode
    n_out = (d_row_new * d_col_new) * n_features

    # set all attributes of this class
    self.set_attr('n_features', n_features)
    self.set_attr('filter', filter)
    self.set_attr('pool_size', pool_size)
    self.set_attr('border_mode', border_mode)
    self.set_attr('ignore_border', ignore_border)
    self.set_attr('d_row', d_row_new)   # number of output row
    self.set_attr('n_out', n_out)   # number of output dimension

    # our CRNN input is 3D tensor that consists of (time, batch, dim)
    # however, the convolution function only accept 4D tensor which is (batch size, stack size, nb row, nb col)
    # therefore, we should convert our input into 4D tensor
    if n_sources != 1:
      if is_conv_layer:
        input = T.concatenate([s.tempOutput for s in self.sources], axis=3)   # (time, batch, input-dim = row * col, stack_size)
        #input = tempInput.reshape((tempInput.shape[0], tempInput.shape[1], tempInput.shape[2] * tempInput.shape[3])) # (time, batch, input-dim = row * col * stack_size)
      else:
        input = T.concatenate([s.output for s in self.sources], axis=2)  # (time, batch, input-dim = row * col * stack_size)
    else:
      input = self.sources[0].output  # (time, batch, input-dim = row * col * stack_size)

    input.name = 'conv_layer_input_concat'
    time = input.shape[0]
    batch = input.shape[1]
    input2 = input.reshape((time * batch, d_row, d_col, stack_size))  # (time * batch, row, col, stack_size)
    self.input = input2.dimshuffle(0, 3, 1, 2)  # (batch, stack_size, row, col)
    self.input.name = 'conv_layer_input_final'

    # filter shape is tuple/list of length 4 which is (nb filters, stack size, filter row, filter col)
    self.filter_shape = (n_features, stack_size, filter, filter)

    # weight parameter
    self.W = self.add_param(self._create_weights(filter_shape=self.filter_shape, pool_size=pool_size))
    # bias parameter
    self.b = self.add_param(self._create_bias(n_features=n_features))

    # when convolutional layer 1x1, it gave the same size even full or valid border mode
    if filter == 1:
      border_mode = 'valid'

    # convolutional function
    # when border mode = same, remove width and height from beginning and last based on the filter size
    if border_mode == 'same':
      new_filter_size = (self.W.shape[2]-1)/2
      self.conv_out = conv.conv2d(
        input=self.input,
        filters=self.W,
        border_mode='full'
      )[:,:,new_filter_size:-new_filter_size,new_filter_size:-new_filter_size]
    else:
      self.conv_out = conv.conv2d(
        input=self.input,
        filters=self.W,
        border_mode=border_mode
      )
    self.conv_out.name = 'conv_layer_conv_out'

    # max pooling function
    self.pooled_out = downsample.max_pool_2d(
      input=self.conv_out,
      ds=pool_size,
      ignore_border=ignore_border
    )
    self.pooled_out.name = 'conv_layer_pooled_out'

    # calculate the convolution output which returns (batch, nb filters, nb row, nb col)
    output = T.tanh(self.pooled_out + self.b.dimshuffle('x', 0, 'x', 'x'))  # (time*batch, filter, out-row, out-col)
    output.name = 'conv_layer_output_plus_bias'

    # our CRNN only accept 3D tensor (time, batch, dim)
    # so, we have to convert the output back to 3D tensor
    output2 = output.dimshuffle(0, 2, 3, 1)  # (time*batch, out-row, out-col, filter)
    self.output = output2.reshape((time, batch, output2.shape[1] * output2.shape[2] * output2.shape[3]))  # (time, batch, out-dim)
    self.tempOutput = output2.reshape((time, batch, output2.shape[1] * output2.shape[2], output2.shape[3]))
    self.make_output(self.output)


  # function for calculating the weight parameter of this class
  def _create_weights(self, filter_shape, pool_size):
    rng = numpy.random.RandomState(23455)
    fan_in = numpy.prod(filter_shape[1:])  # stack_size * filter_row * filter_col
    fan_out = (filter_shape[0] * numpy.prod(filter_shape[2:]) / numpy.prod(pool_size))  # (n_features * (filter_row * filter_col)) / (pool_size[0] * pool_size[1])

    W_bound = numpy.sqrt(6. / (fan_in + fan_out))
    return theano.shared(
      numpy.asarray(
        rng.uniform(low=-W_bound, high=W_bound, size=filter_shape),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="W_conv"
    )

  # function for calculating the bias parameter of this class
  def _create_bias(self, n_features):
    return theano.shared(
      numpy.zeros(
        (n_features,),
        dtype=theano.config.floatX
      ),
      borrow=True,
      name="b_conv"
    )

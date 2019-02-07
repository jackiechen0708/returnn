import torch
from torch.autograd import grad
from PTUpdater import Updater
from PTNetwork import LayerNetwork

floatX = 'float32'

class Device(object):
  def __init__(self, device, config, blocking=False, num_batches=1, update_specs=None):
    """
    :param str device: name, "gpu*" or "cpu*"
    :param Config.Config config: config
    :param bool blocking: False -> multiprocessing, otherwise its blocking
    :param int num_batches: num batches to train on this device
    :param dict update_specs
    """
    self.num_batches = num_batches
    self.blocking = blocking
    self.config = config
    self.output = None; " :type: list[numpy.ndarray] "
    self.outputs_format = None; " :type: list[str] "  # via self.result()
    self.train_outputs_format = None; " :type: list[str] "  # set via self.initialize()
    self.run_called_count = 0
    self.result_called_count = 0
    self.wait_for_result_call = False
    self.compute_total_time = 0
    self.update_total_time = 0
    self.num_frames = NumbersDict(0)
    self.num_updates = 0
    self.epoch = None
    self.use_inputs = False
    if not update_specs: update_specs = {}
    update_specs.setdefault('update_rule', 'global')
    update_specs.setdefault('update_params', {})
    update_specs.setdefault('layers', [])
    update_specs.setdefault('block_size', 0)
    self.update_specs = update_specs
    self.main_pid = os.getpid()

    self.name = device
    self.initialized = False
    start_new_thread(self.startProc, (device,))

  def __str__(self):
    async_str = "async (pid %i, ppid %i)" % (os.getpid(), os.getppid())
    return "<Device %s %s>" % (self.name, async_str)

  def startProc(self, *args, **kwargs):
    import better_exchook
    better_exchook.install()
    try:
      self._startProc(*args, **kwargs)
    except BaseException:
      try:
        sys.excepthook(*sys.exc_info())
      finally:
        # Exceptions are fatal. Stop now.
        interrupt_main()

  def _startProc(self, device_tag):
    assert not self.blocking
    if self.name[0:3] == "cpu":
      dev = torch.device("cpu")
    else:
      dev = torch.device("cuda:%d" % int(device[3:]))
    self.proc = AsyncTask(
      func=self.process,
      name="Device %s proc" % self.name,
      mustExec=True,
      env_update=env_update)
    # The connection (duplex pipe) is managed by AsyncTask.
    self.input_queue = self.output_queue = self.proc.conn

    try:
      self.id = self.output_queue.recv(); """ :type: int """
      self.device_name = self.output_queue.recv(); """ :type: str """
      self.num_train_params = self.output_queue.recv(); """ :type: int """  # = len(trainnet.gparams)
      self.sync_used_targets()
    except ProcConnectionDied as e:
      print("Device proc %s (%s) died: %r" % (self.name, device_tag, e), file=log.v3)
      interrupt_main()
    if self.device_name in get_device_attributes().keys():
      self.attributes = get_device_attributes()[self.device_name]
    else:
      self.attributes = get_device_attributes()['default']
    self.name = device_tag[0:3] + str(self.id)
    self.initialized = True

  def detect_nan(self, i, node, fn):
    for output in fn.outputs:
      if numpy.isnan(output[0]).any():
        #theano.printing.debugprint(node)
        print(('Inputs : %s' % [input[0] for input in fn.inputs]))
        print(('Outputs: %s' % [output[0] for output in fn.outputs]))
        assert False, '*** NaN detected ***'

  def initialize(self, config, update_specs=None, json_content=None, train_param_args=None):
    """
    :type config: Config.Config
    :type json_content: dict[str] | str | None
    :type train_param_args: dict | None
    """
    if not update_specs: update_specs = {}
    update_specs.setdefault('update_rule', 'global')
    update_specs.setdefault('update_params', {})
    update_specs.setdefault('block_size', 0)
    update_specs.setdefault('layers', [])
    self.update_specs = update_specs
    self.block_size = update_specs['block_size']
    self.total_cost = 0
    target = config.value('target', 'classes')
    assert os.getpid() != self.main_pid # this won't work on Windows
    import h5py
    self.seq_train_parallel_control = None  # type: SeqTrainParallelControlDevHost. will be set via SprintErrorSignals
    self.network_task = config.value('task', 'train')
    eval_flag = self.network_task in ['eval', 'forward', 'daemon']
    testnet_kwargs = dict(mask="unity", train_flag=False, eval_flag=eval_flag)
    self.testnet_share_params = config.bool("testnet_share_params", False)
    if self.testnet_share_params:
      testnet_kwargs["shared_params_network"] = lambda: self.trainnet
    if json_content is not None:
      self.trainnet = PTLayerNetwork.from_json_and_config(json_content, config, train_flag=True, eval_flag=False)
      self.testnet = PTLayerNetwork.from_json_and_config(json_content, config, **testnet_kwargs)
    elif config.bool('initialize_from_model', False) and config.has('load'):
      model = h5py.File(config.value('load', ''), "r")
      self.trainnet = PTLayerNetwork.from_hdf_model_topology(model, train_flag=True, eval_flag=False,
                                                            **PTLayerNetwork.init_args_from_config(config))
      self.testnet = PTLayerNetwork.from_hdf_model_topology(model, **dict_joined(testnet_kwargs,
                                                            PTLayerNetwork.init_args_from_config(config)))
      model.close()
    else:
      self.trainnet = PTLayerNetwork.from_config_topology(config, train_flag=True, eval_flag=False)
      self.testnet = PTLayerNetwork.from_config_topology(config, **testnet_kwargs)
    if train_param_args is not None:
      self.trainnet.declare_train_params(**train_param_args)
    if self.testnet_share_params:
      testnet_all_params = self.testnet.get_all_params_vars()
      assert len(testnet_all_params) == 0
    if config.has('load'):
      model = h5py.File(config.value('load', ''), "r")
      if 'update_step'in model.attrs:
        self.trainnet.update_step = model.attrs['update_step']
      model.close()
    # initialize batch
    self.used_data_keys = self.trainnet.get_used_data_keys()
    print("Device train-network: Used data keys:", self.used_data_keys, file=log.v4)
    assert "data" in self.used_data_keys
    # TODO
    #self.y = {k: theano.shared(numpy.zeros((1,) * self.trainnet.y[k].ndim, dtype=self.trainnet.y[k].dtype),
    #                           borrow=True, name='y_%s' % k)
    #          for k in self.used_data_keys}
    #self.j = {k: theano.shared(numpy.zeros((1, 1), dtype='int8'), borrow=True, name='j_%s' % k)
    #          for k in self.used_data_keys}
    if self.network_task == 'train':
      gparams = []
      exclude = []
      self.gradients = {}; ":type: dict[theano.SharedVariable,theano.Variable]"
      for pi, param in enumerate(self.trainnet.train_params_vars):
        if log.verbose[4]: progress_bar(float(pi) / len(self.trainnet.train_params_vars), "calculating gradients ...")
        if hasattr(param,'custom_update'):
          gparam = param.custom_update
        elif update_specs['layers'] and param.layer.name not in update_specs['layers']:
          gparam = 0
        else:
          if param.layer.attrs.get('cost',''):
            keys = param.layer.attrs['cost']
            if isinstance(keys, list):
              gparam = sum(grad(self.trainnet.cost[k],param))
            else:
              gparam = grad(self.trainnet.costs[param.layer.attrs['cost']], param)
          else:
            gparam = grad(self.trainnet.get_objective(), param)
        if gparam == 0:
          exclude.append(param)
          print("exclude:", self.name, param.name, file=log.v4)
          gparams.append(T.constant(0))
          continue
        self.gradients[param] = gparam
        gparams.append(gparam)
    else:
      self.gradients = None
    if log.verbose[4]: progress_bar()

    # initialize functions
    self.updater = None
    self.update_specs = update_specs

    self.forwarder = None
    self.use_inputs = False
    if self.network_task  == 'train':
        train_givens = self.make_givens(self.trainnet)
        test_givens = self.make_givens(self.testnet)

      if self.update_specs['update_rule'] == 'global':
        self.updater = Updater.initFromConfig(self.config)
      elif self.update_specs['update_rule'] != 'none':
        self.updater = Updater.initRule(self.update_specs['update_rule'], **self.update_specs['update_params'])

      outputs = []
      self.train_outputs_format = []
      if config.float('batch_pruning', 0):
        outputs = [self.trainnet.output["output"].seq_weight]
        self.train_outputs_format = ["weights"]

      self.train_outputs_format += ["cost:" + out for out in sorted(self.trainnet.costs.keys())]
      self.updater.initVars(self.trainnet, self.gradients)

      # TODO
      #self.trainer = self.trainnet.theano.function(inputs=[self.block_start, self.block_end],
      #                               outputs=outputs,
      #                               givens=train_givens,
      #                               updates=self.updater.getUpdateList(),
      #                               on_unused_input=config.value('theano_on_unused_input', 'ignore'),
      #                               no_default_updates=exclude,
      #                               name="train_and_updater")

      self.test_outputs_format = ["cost:" + out for out in sorted(self.testnet.costs.keys())]
      self.test_outputs_format += ["error:" + out for out in sorted(self.testnet.errors.keys())]
      test_outputs = output_streams['eval'] + [self.testnet.costs[out] for out in sorted(self.testnet.costs.keys())]
      test_outputs += [self.testnet.errors[out] for out in sorted(self.testnet.errors.keys())]
      # TODO
      #self.tester = theano.function(inputs=[self.block_start, self.block_end],
      #                              outputs=test_outputs,
      #                              givens=test_givens,
      #                              on_unused_input=config.value('theano_on_unused_input', 'ignore'),
      #                              no_default_updates=True,
      #                              name="tester")
    elif self.network_task == 'forward':
      output_layer_name = config.value("extract_output_layer_name", "output")
      extractions = config.list('extract', ['log-posteriors'])
      givens = self.make_input_givens(self.testnet)
      for extract in extractions:
        param = None
        if ':' in extract:
          param = extract.split(':')[1]
          extract = extract.split(':')[0]
        elif extract == "log-posteriors":
          pass #TODO
        else:
          assert False, "invalid extraction: " + extract

      # TODO
      # self.extractor = theano.function(inputs = [],
      #                                  outputs = source if len(source) == 1 else [T.concatenate(source, axis=-1)],
      #                                  givens = givens,
      #                                  name = "extractor")

  def compute_run(self, task):
    compute_start_time = time.time()
    self.compute_start_time = compute_start_time
    batch_dim = self.y["data"].get_value(borrow=True, return_internal_type=True).shape[1]
    block_size = self.block_size if self.block_size else batch_dim
    if self.config.bool("debug_shell_first_compute", False):
      print("debug_shell_first_compute", file=log.v1)
      Debug.debug_shell(user_ns=locals(), user_global_ns=globals())
    if task == "train" or task == "theano_graph" or task == "eval":
      func = self.tester if task == "eval" else self.trainer
      output = []
      batch_end = 0
      while batch_end < batch_dim:
        batch_start = batch_end
        batch_end = min(batch_start + block_size, batch_dim)
        block_output = func(batch_start, batch_end)
        if not output:
          output = block_output
        else:
          for j in range(len(block_output)):
            output[j] += block_output[j]
    elif task == "extract" or task == "forward":
      if self.use_inputs:
        inp = [self.y[k].get_value() for k in self.used_data_keys]
        inp += [self.j[k].get_value() for k in self.used_data_keys]
        output = self.extractor(*inp)
      else:
        output = self.extractor()
      if self.save_graph:
        import dill
        sys.setrecursionlimit(50000)
        graphfile = self.config.value('save_graph','')
        print("Loading pre-compiled graph from '%s'" % graphfile, file=log.v4)
        dill.dump(self.extractor, open(graphfile, 'wb'))
        self.save_graph = False
    elif task == 'classify':
      output = self.classifier()
    elif task == "analyze":
      output = self.analyzer()
    else:
      assert False, "invalid command: " + task
    compute_end_time = time.time()
    if self.config.bool("debug_batch_compute_time", False):
      print("batch compute time:", compute_end_time - compute_start_time, file=log.v1)
    self.compute_total_time += compute_end_time - compute_start_time
    # output is a list the outputs which we specified when creating the Theano function in self.initialize().
    assert len(output) > 0  # In all cases, we have some output.
    outputs_format = None
    if task.startswith("train"):
      outputs_format = self.train_outputs_format
    elif task == "eval":
      outputs_format = self.test_outputs_format

    # stream handling
    if(self.streams):
      stream_outputs = output[:len(self.streams)]
      output = output[len(self.streams):]
      for stream, out in zip(self.streams, stream_outputs):
        stream.update(task, out, self.tags)

    if outputs_format:
      for fmt, out in zip(outputs_format, output):
        if fmt.startswith('cost:'):
          self.total_cost += out

    # In train, first output is the score.
    # If this is inf/nan, our model is probably broken.
    model_broken_short_info = self.fast_check_model_is_broken_from_result(output, outputs_format)
    if model_broken_short_info:
      print("Model looks broken:", model_broken_short_info, file=log.v3)
      if self.config.bool("dump_model_broken_info", False):
        self.dump_model_broken_info(model_broken_short_info)
      if self.config.bool("debug_shell_model_broken", False):
        print("debug_shell_model_broken", file=log.v1)
        Debug.debug_shell(user_ns=locals(), user_global_ns=globals())
    # Pass on, let the Engine decide what to do (or also just fail).

    return output, outputs_format

  def get_compute_func(self, task):
    if task == "train":
      return self.trainer
    raise NotImplementedError("for task: %r" % task)

  def fast_check_model_is_broken_from_result(self, output, outputs_format):
    if not outputs_format:  # In train, we should always have this.
      return
    output_dict = self.make_result_dict(output, outputs_format)
    # Check only params which are small, i.e. not the whole gparams.
    RelevantAttribs = ["cost", "gradient_norm"]
    def is_relevant_attrib(k):
      for rk in RelevantAttribs:
        if k == rk or k.startswith(rk + ":"):
          return True
      return False
    values = {k: numpy.asarray(v)
              for k, v in output_dict.items() if is_relevant_attrib(k)}
    for attrib, value in values.items():
      if not numpy.isfinite(value).all():
        return ", ".join(["%s = %s" % (k, v) for (k, v) in values.items()])
    return

  def dump_model_broken_info(self, info):
    try:
      dump_file_name = "model_broken_dump.pickle.log"
      if os.path.exists(dump_file_name):
        i = 1
        while os.path.exists("%s.%i" % (dump_file_name, i)):
          i += 1
        dump_file_name = "%s.%i" % (dump_file_name, i)
      f = open(dump_file_name, "w")
      print("Dumping model broken info to file %r." % dump_file_name, file=log.v1)
    except Exception as e:
      print("Exception while opening model broken dump file. %s" % e, file=log.v3)
      return
    collected_info = {"info_str": str(info)}
    try:
      collected_info["dev_data"] = numpy.asarray(self.y["data"].get_value())
      collected_info["dev_targets"] = numpy.asarray(self.y["classes"].get_value()) #TODO fix for multiple targets with other labels
      collected_info["dev_index"] = numpy.asarray(self.j["data"].get_value())
    except Exception as e:
      print("Exception when getting device data. %s" % e, file=log.v3)
    try:
      train_params = [numpy.asarray(v.get_value()) for v in self.trainnet.train_params_vars]
      collected_info["train_params"] = train_params
    except Exception as e:
      print("Exception when getting train params. %s" % e, file=log.v3)
    try:
      pickle.dump(collected_info, f)
      f.close()
    except Exception as e:
      print("Exception when writing model broken info dump. %s" % e, file=log.v3)

  def process(self, asyncTask):
    """
    :type asyncTask: AsyncTask
    """
    device = self.name
    config = self.config
    global asyncChildGlobalDevice, deviceInstance
    asyncChildGlobalDevice = self
    deviceInstance = self
    try:
      # We do some minimal initialization, modelled after rnn.init().
      # This is needed because we are a new independent process. See startProc().
      import rnn
      rnn.initBetterExchook()
      rnn.config = config
      rnn.initLog()
      print("Device %s proc starting up, pid %i" % (device, os.getpid()), file=log.v3)
      print("Device %s proc: THEANO_FLAGS = %r" % (device, os.environ.get("THEANO_FLAGS", None)), file=log.v4)
      rnn.initFaulthandler()
      rnn.initConfigJsonNetwork()
      self.process_inner(device, config, self.update_specs, asyncTask)
    except ProcConnectionDied as e:
      print("Device %s proc, pid %i: Parent seem to have died: %s" % (device, os.getpid(), e), file=log.v2)
      sys.exit(1)
    except KeyboardInterrupt:
      # Killed by parent.
      print("Device %s proc got KeyboardInterrupt" % device, file=log.v4)
      sys.exit(1)
    except Exception as e:
      print("Device %s proc exception: %s" % (device, e), file=log.v2)
      sys.excepthook(*sys.exc_info())
      sys.exit(1)

  def process_inner(self, device, config, update_specs, asyncTask):
    """
    :type device: str
    :type config: Config.Config
    :type asyncTask: AsyncTask
    """
    # The connection (duplex pipe) is managed by AsyncTask.
    # TODO: pytorhc initialization of given device for this process
    output_queue.send(device_id) # TODO
    output_queue.send(device_name) # TODO

    custom_dev_init_code = config.value('custom_dev_init_code', None, list_join_str="\n")
    if custom_dev_init_code:
      custom_exec(custom_dev_init_code, "<custom dev init code string>", {}, dict_joined(globals(), locals()))

    self.initialize(config, update_specs=update_specs)
    #self._checkGpuFuncs(device, device_id)
    output_queue.send(len(self.trainnet.train_params_vars))
    print("Device %s proc, pid %i is ready for commands." % (device, os.getpid()), file=log.v4)
    network_params = []
    while True:
      cmd = input_queue.recv()
      if cmd == "stop":  # via self.terminate()
        output_queue.send("done")
        break
      elif cmd == "generic-exec":
        args = input_queue.recv()
        res = self._generic_exec(*args)
        output_queue.send("generic-exec-result")
        output_queue.send(res)
      elif cmd == "reset":  # via self.reset()
        self.epoch = input_queue.recv()
        self.epoch_var.set_value(self.epoch)
        if self.updater:
          self.updater.reset()
      elif cmd == "reinit":  # via self.reinit()
        json_content = input_queue.recv()
        train_param_args = input_queue.recv()
        if self.need_reinit(json_content=json_content, train_param_args=train_param_args):
          self.initialize(config, update_specs=update_specs,
                          json_content=json_content, train_param_args=train_param_args)
        output_queue.send("reinit-ready")
        output_queue.send(len(self.trainnet.train_params_vars))
      elif cmd == "update-data":  # via self.update_data()
        t = {}
        target_keys = input_queue.recv()
        for k in target_keys:
          t[k] = input_queue.recv()
        self.output_index = {}
        for k in target_keys:
          self.output_index[k] = input_queue.recv()
        self.tags = input_queue.recv()
        update_start_time = time.time()
        # self.x == self.y["data"], will be set also here.
        for k in target_keys:
          self.y[k].set_value(t[k].astype(self.y[k].dtype), borrow = True)
        #self.c.set_value(c.astype('int32'), borrow = True)
        for k in target_keys:
          self.j[k].set_value(self.output_index[k].astype('int8'), borrow = True)
        try:
          self.tags_var.set_value(numpy.array(self.tags).view(dtype='int8').reshape((len(self.tags), max(map(len, self.tags)))))
        except:
          tags = [s.encode('utf-8') for s in self.tags]
          self.tags_var.set_value(numpy.array(tags).view(dtype='int8').reshape((len(tags), max(map(len, tags)))))
        self.update_total_time += time.time() - update_start_time
      elif cmd == "set-learning-rate":  # via self.set_learning_rate()
        learning_rate = input_queue.recv()
        if self.updater:
          self.updater.setLearningRate(learning_rate)
      elif cmd == "set-net-params":  # via self.set_net_params()
        self.total_cost = 0
        our_params_trainnet = self.trainnet.get_all_params_vars()
        our_params_testnet = self.testnet.get_all_params_vars()
        assert isinstance(our_params_trainnet, list)
        params_len = input_queue.recv()
        params = [input_queue.recv_bytes() for i in range(params_len)]
        assert input_queue.recv() == "end-set-net-params"
        assert len(params) == len(our_params_trainnet)
        if self.testnet_share_params:
          assert len(our_params_testnet) == 0
        else:
          assert len(params) == len(our_params_testnet)
        for i in range(params_len):
          param_str = params[i]
          param = numpy.fromstring(param_str, dtype=floatX)
          our_p_train = our_params_trainnet[i]
          our_param_shape = our_p_train.get_value(borrow=True, return_internal_type=True).shape
          assert numpy.prod(our_param_shape) == numpy.prod(param.shape)
          #assert numpy.isfinite(param).all()
          converted = param.reshape(our_param_shape)
          our_p_train.set_value(converted)
          if not self.testnet_share_params:
            our_params_testnet[i].set_value(converted)
      elif cmd == 'get-num-updates':
        if self.updater:
          output_queue.send(int(self.updater.i.get_value()))
        else:
          output_queue.send(0)
      elif cmd == 'get-total-cost':
        output_queue.send(self.total_cost)
      elif cmd == "get-net-train-params":  # via self.get_net_train_params()
        output_queue.send("net-train-params")
        output_queue.send(len(network_params))
        for p in network_params:
          output_queue.send_bytes(p)
        output_queue.send("end-get-net-train-params")
      elif cmd == "sync-net-train-params":
        network_params = []
        for p in self.trainnet.get_all_params_vars():
          network_params.append(numpy.asarray(p.get_value(), dtype=floatX).tostring())
      elif cmd == "task":  # via self.run()
        task = input_queue.recv()
        try:
          output, outputs_format = self.compute_run(task)
        except RuntimeError:
          print("warning: Runtime error on device", device_name, file=log.v2)
          output_queue.send("error")
          sys.excepthook(*sys.exc_info())
          # If there are any other than the main thread.
          # Actually, that would be unexpected.
          Debug.dumpAllThreadTracebacks()
          return
        except MemoryError:
          output_queue.send("error")
          raise
        output_queue.send("task-result")
        # We can get cuda_ndarray or other references to internal device memory.
        # We explicitly want to copy them over to CPU memory.
        output_queue.send([numpy.asarray(v) for v in output])
        #output_queue.send(output)
        output_queue.send(outputs_format)
      else:
        raise Exception("cmd %s unknown" % cmd)

  def sync_net_train_params(self):
    if not self.blocking:
      self.input_queue.send("sync-net-train-params")

  def get_net_train_params(self, network):
    if self.blocking:
      return [v.get_value(borrow=True, return_internal_type=True) for v in self.trainnet.get_all_params_vars()]
    else:
      assert self.main_pid == os.getpid()
      self.input_queue.send("get-net-train-params")
      r = self.output_queue.recv()
      assert r == "net-train-params"
      param_count = self.output_queue.recv()
      assert param_count == len(network.get_all_params_vars())
      raw = [self.output_queue.recv_bytes() for i in range(param_count)]
      assert self.output_queue.recv() == "end-get-net-train-params"
      vars = network.get_all_params_vars()
      res = []
      assert len(vars) == len(raw)
      for p,q in zip(vars, raw):
        res.append(numpy.fromstring(q, dtype=floatX).reshape(p.get_value().shape))
      return res

  def set_net_encoded_params(self, network_params):
    """
    :type network_params: list[numpy.ndarray]
    This updates *all* params, not just the train params.
    """
    assert not self.blocking
    self.input_queue.send("set-net-params")
    self.input_queue.send(len(network_params))
    for p in network_params:
      self.input_queue.send_bytes(p.astype(floatX).tostring())
    self.input_queue.send("end-set-net-params")

  def set_net_params(self, network):
    """
    :type network: Network.LayerNetwork
    This updates *all* params, not just the train params.
    """
    if self.blocking:
      self.trainnet.set_params_by_dict(network.get_params_dict())
      if not self.testnet_share_params:
        self.testnet.set_params_by_dict(network.get_params_dict())
    else:
      assert self.main_pid == os.getpid()
      self.set_net_encoded_params([
        numpy.asarray(p.get_value()) for p in network.get_all_params_vars()])

  def is_device_proc(self):
    return self.main_pid != os.getpid()

  def get_task_network(self):
    """
    :rtype: LayerNetwork
    """
    if self.network_task == "train":
      return self.trainnet
    else:
      return self.testnet

  def _host__get_used_targets(self):
    assert self.is_device_proc()
    return self.used_data_keys

  def sync_used_targets(self):
    """
    Updates self.used_targets for the host.
    """
    if self.is_device_proc():
      return  # Nothing to do.
    self.used_data_keys = self._generic_exec_on_dev("_host__get_used_targets")  # type: list[str]

  def alloc_data(self, shapes, max_ctc_length=0):
    """
    :param dict[str,list[int]] shapes: by data-key. format usually (time,batch,features)
    :type max_ctc_length: int
    """
    assert self.main_pid == os.getpid()
    assert all([s > 0 for s in shapes["data"]])
    self.targets = {k: numpy.full(shapes[k], -1, dtype=floatX) for k in self.used_data_keys}
    self.output_index = {k: numpy.zeros(shapes[k][0:2], dtype='int8') for k in self.used_data_keys}
    self.tags = [None] * shapes["data"][1]  # type: list[str]  # seq-name for each batch slice

  def update_data(self):
    # self.data is set in Engine.allocate_devices()
    if self.blocking:
      update_start_time = time.time()
      for target in self.used_data_keys:
        self.y[target].set_value(self.targets[target].astype(self.y[target].dtype), borrow = True)
      for k in self.used_data_keys:
        self.j[k].set_value(self.output_index[k], borrow = True)
      self.update_total_time += time.time() - update_start_time
    else:
      assert self.main_pid == os.getpid()
      self.input_queue.send("update-data")
      target_keys = list(sorted(self.used_data_keys))
      self.input_queue.send(target_keys)
      for target in target_keys:
        self.input_queue.send(self.targets[target])
      for k in target_keys:
        self.input_queue.send(self.output_index[k])
      self.input_queue.send(self.tags)
      if self.config.value('loss','') in ('ctc', 'hmm'):
        self.input_queue.send(self.ctc_targets)

  def set_learning_rate(self, learning_rate):
    """
    :type learning_rate: float
    """
    if self.blocking:
      if self.updater:
        self.updater.setLearningRate(learning_rate)
    else:
      assert self.main_pid == os.getpid()
      self.input_queue.send("set-learning-rate")
      self.input_queue.send(learning_rate)

  def get_num_updates(self):
    if self.blocking:
      if self.updater:
        return self.updater.i.get_value()
      else:
        return 0
    else:
      assert self.main_pid == os.getpid()
      self.input_queue.send("get-num-updates")
      return int(self.output_queue.recv())

  def get_total_cost(self):
    if self.blocking:
      return self.total_cost
    else:
      assert self.main_pid == os.getpid()
      self.input_queue.send("get-total-cost")
      return float(self.output_queue.recv())

  def start_epoch_stats(self):
    if not self.is_device_proc():
      return self._generic_exec_on_dev("start_epoch_stats")
    self.epoch_start_time = time.time()
    self.compute_total_time = 0
    self.update_total_time = 0

  def finish_epoch_stats(self):
    if not self.is_device_proc():
      return self._generic_exec_on_dev("finish_epoch_stats")
    cur_time = time.time()
    total_time = cur_time - self.epoch_start_time
    total_time = max(total_time, 0.001)
    compute_frac = self.compute_total_time / total_time
    update_frac = self.update_total_time / total_time
    print("Device %s proc epoch time stats: total %s, %.02f%% computing, %.02f%% updating data" % \
                     (self.name, hms(total_time), compute_frac * 100, update_frac * 100), file=log.v4)

  def need_reinit(self, json_content, train_param_args=None):
    if self.config.bool('reinit', True) == False:
      return False
    assert self.trainnet
    if isinstance(json_content, str):
      import json
      json_content = json.loads(json_content)
    if self.trainnet.to_json_content() != json_content:
      print("Device: reinit because network description differs. Diff:", \
                       dict_diff_str(self.trainnet.to_json_content(), json_content), file=log.v3)
      return True
    if train_param_args is None:
      train_param_args = self.trainnet.get_train_param_args_default()
    if self.trainnet.train_param_args != train_param_args:
      print("Device: reinit because network train params differ", file=log.v3)
      return True
    return False

  def reinit(self, json_content, train_param_args=None):
    """
    :type json_content: dict[str] | str
    :type train_param_args: dict
    :returns len of train_params
    :rtype: int
    Reinits for a new network topology. This can take a while
    because the gradients have to be recomputed.
    """
    assert self.main_pid == os.getpid(), "Call this from the main proc."
    if self.blocking:
      if self.need_reinit(json_content=json_content, train_param_args=train_param_args):
        self.initialize(self.config, update_specs=self.update_specs,
                        json_content=json_content,
                        train_param_args=train_param_args)
      return len(self.trainnet.train_params_vars)
    else:
      self.input_queue.send("reinit")
      self.input_queue.send(json_content)
      self.input_queue.send(train_param_args)
      r = self.output_queue.recv()
      assert r == "reinit-ready"
      r = self.output_queue.recv()
      self.sync_used_targets()
      return r

  def prepare(self, network, updater=None, train_param_args=None, epoch=None):
    """
    Call this from the main proc before we do anything else.
    This is called before we start any training, e.g. at the begin of an epoch.
    :type network: LayerNetwork
    :type updater: Updater | None
    :type train_param_args: dict | None
    """
    assert self.main_pid == os.getpid(), "Call this from the main proc."
    # Reinit if needed.
    self.reinit(json_content=network.to_json_content(), train_param_args=train_param_args)
    self.set_net_params(network)
    self.epoch = epoch
    if self.blocking:
      self.epoch_var.set_value(epoch)
      if self.updater:
        self.updater.reset()
    else:
      self.input_queue.send('reset')
      self.input_queue.send(epoch)

  def run(self, task):
    """
    :type task: str
    """
    self.task = task
    self.run_called_count += 1
    self.update_data()
    assert not self.wait_for_result_call
    self.wait_for_result_call = True
    if self.blocking:
      self.output, self.outputs_format = self.compute_run(task)
    else:
      assert self.main_pid == os.getpid()
      self.output = None
      self.outputs_format = None
      self.input_queue.send("task")
      self.input_queue.send(task)

  def clear_memory(self, network):
    self.update_data()

  @staticmethod
  def make_result_dict(output, outputs_format):
    """
    :type output: list[numpy.ndarray]
    :type outputs_format: list[str]
    """
    assert len(output) == len(outputs_format)
    return dict(zip(outputs_format, output))

  def result(self):
    """
    :rtype: (list[numpy.ndarray], list[str] | None)
    :returns the outputs and maybe a format describing the output list
    See self.make_result_dict() how to interpret this list.
    See self.initialize() where the list is defined.
    """
    assert self.wait_for_result_call
    self.result_called_count += 1
    if self.blocking:
      assert self.result_called_count == self.run_called_count
      self.wait_for_result_call = False
      return self.output, self.outputs_format
    else:
      assert self.main_pid == os.getpid()
      assert self.result_called_count <= self.run_called_count
      if not self.proc.is_alive():
        print("Dev %s proc not alive anymore" % self.name, file=log.v4)
        return None, None
      # 60 minutes execution timeout by default
      timeout = self.config.float("device_timeout", 60 * 60)
      while timeout > 0:
        try:
          if self.output_queue.poll(1):
            r = self.output_queue.recv()
            if r == "error":
              print("Dev %s proc reported error" % self.name, file=log.v5)
              self.wait_for_result_call = False
              return None, None
            assert r == "task-result"
            output = self.output_queue.recv()
            outputs_format = self.output_queue.recv()
            assert output is not None
            self.wait_for_result_call = False
            return output, outputs_format
        except ProcConnectionDied as e:
          # The process is dying or died.
          print("Dev %s proc died: %s" % (self.name, e), file=log.v4)
          self.wait_for_result_call = False
          return None, None
        timeout -= 1
      print("Timeout (device_timeout = %s) expired for device %s" % (self.config.float("device_timeout", 60 * 60), self.name), file=log.v3)
      try:
        os.kill(self.proc.proc.pid, signal.SIGUSR1)
      except Exception as e:
        print("os.kill SIGUSR1 exception: %s" % e, file=log.v3)
      return None, None

  def forward(self, use_trainnet=False):
    assert self.is_device_proc()
    network = self.trainnet if use_trainnet else self.testnet
    import theano
    if not self.forwarder:
      print("Device: Create forwarder, use trainnet:", use_trainnet, ", testnet_share_params:", self.testnet_share_params, file=log.v3)
      self.forwarder = theano.function(
        inputs=[],
        outputs=[layer.output for name, layer in sorted(network.output.items())],
        givens=self.make_input_givens(network),
        on_unused_input='warn',
        name="forwarder")
    from TheanoUtil import make_var_tuple
    outputs = make_var_tuple(self.forwarder())
    assert len(outputs) == len(network.output)
    return {name: outputs[i] for i, (name, layer) in enumerate(sorted(network.output.items()))}

  def terminate(self):
    if self.blocking:
      return
    if not self.proc:
      return
    if not self.proc.is_alive():
      return
    assert self.main_pid == os.getpid()
    try:
      self.input_queue.send('stop')
    except ProcConnectionDied:
      pass
    self.proc.join(timeout=10)
    self.proc.terminate()
    self.proc = None
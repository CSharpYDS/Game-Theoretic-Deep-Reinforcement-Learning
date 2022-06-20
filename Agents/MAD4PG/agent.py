"""D3PG agent implementation."""
import sys
sys.path.append(r"/home/neardws/Documents/Game-Theoretic-Deep-Reinforcement-Learning/")

import copy
import dataclasses
from typing import Callable, Iterator, List, Optional, Tuple, Union, Sequence
import acme
from acme import adders
from acme import core
from acme import datasets
from acme import types
from acme.adders import reverb as reverb_adders
from Agents.MAD4PG import actors
from Agents.MAD4PG import learning
from acme.agents import agent
from acme.tf import networks as network_utils
from acme.tf import utils
from acme.tf import variable_utils
from acme.tf import savers as tf2_savers
from acme.utils import counting
from acme.utils import loggers
from acme.utils import lp_utils
import tensorflow as tf
import reverb
import sonnet as snt
import launchpad as lp
import functools
import dm_env
from Agents.MAD4PG.networks import make_default_MAD3PGNetworks

Replicator = Union[snt.distribute.Replicator, snt.distribute.TpuReplicator]

# Valid values of the "accelerator" argument.
_ACCELERATORS = ('GPU', 'TPU')

@dataclasses.dataclass
class MAD3PGConfig:
    """Configuration options for the MAD3PG agent.
    Args:
        discount: discount to use for TD updates.
        batch_size: batch size for updates.
        prefetch_size: size to prefetch from replay.
        target_update_period: number of learner steps to perform before updating
            the target networks.
        policy_optimizer: optimizer for the policy network updates.
        critic_optimizer: optimizer for the critic network updates.
        min_replay_size: minimum replay size before updating.
        max_replay_size: maximum replay size.
        samples_per_insert: number of samples to take from replay for every insert
            that is made.
        n_step: number of steps to squash into a single transition.
        sigma: standard deviation of zero-mean, Gaussian exploration noise.
        clipping: whether to clip gradients by global norm.
        replay_table_name: string indicating what name to give the replay table.
        counter: counter object used to keep track of steps.
        logger: logger object to be used by learner.
        checkpoint: boolean indicating whether to checkpoint the learner.
        accelerator: 'TPU', 'GPU', or 'CPU'. If omitted, the first available accelerator type from ['TPU', 'GPU', 'CPU'] will be selected.
    """
    discount: float = 0.99
    batch_size: int = 256
    prefetch_size: int = 4
    target_update_period: int = 4
    policy_optimizers: List[snt.Optimizer] = dataclasses.field(default_factory=list)
    critic_optimizers: List[snt.Optimizer] = dataclasses.field(default_factory=list)
    min_replay_size: int = 1000
    max_replay_size: int = 1000000
    samples_per_insert: Optional[float] = 1.0
    n_step: int = 5
    sigma: float = 0.3
    clipping: bool = True
    replay_table_name: str = reverb_adders.DEFAULT_PRIORITY_TABLE
    counter: Optional[counting.Counter] = None
    logger: Optional[loggers.Logger] = None
    checkpoint: bool = True
    accelerator: Optional[str] = 'GPU'


@dataclasses.dataclass
class MAD3PGNetwork:
    """Structure containing the networks for MAD3PG."""
    
    policy_network: types.TensorTransformation
    critic_network: types.TensorTransformation
    observation_network: types.TensorTransformation

    def __init__(
        self,
        policy_network: types.TensorTransformation,
        critic_network: types.TensorTransformation,
        observation_network: types.TensorTransformation,
    ):
        # This method is implemented (rather than added by the dataclass decorator)
        # in order to allow observation network to be passed as an arbitrary tensor
        # transformation rather than as a snt Module.
        self.policy_network = policy_network
        self.critic_network = critic_network
        self.observation_network = utils.to_sonnet_module(observation_network)

    def init(
        self, 
        environment_spec,
    ):
        """Initialize the networks given an environment spec."""
        # Get observation and action specs.
        observation_spec = environment_spec.edge_observation
        critic_action_spec = environment_spec.critic_actions

        # Create variables for the observation net and, as a side-effect, get a
        # spec describing the embedding space.
        emb_spec = utils.create_variables(self.observation_network, [observation_spec])
        
        # Create variables for the policy and critic nets.
        _ = utils.create_variables(self.policy_network, [emb_spec])
        _ = utils.create_variables(self.critic_network, [emb_spec, critic_action_spec])
        

    def make_policy(
        self,
        environment_spec,
        sigma: float = 0.0,
    ) -> snt.Module:
        """Create a single network which evaluates the policy."""
        # Stack the observation and policy networks.

        stacks = [self.observation_network, self.policy_network]

        # If a stochastic/non-greedy policy is requested, add Gaussian noise on
        # top to enable a simple form of exploration.
        # TODO: Refactor this to remove it from the class.
        if sigma > 0.0:
            stacks += [
                network_utils.ClippedGaussian(sigma),
                network_utils.ClipToSpec(environment_spec.actions),   # Clip to action spec.
            ]
                
        # Return a network which sequentially evaluates everything in the stack.
        return snt.Sequential(stacks)


class MAD3PGAgent(agent.Agent):
    """D3PG Agent.
    This implements a single-process D3PG agent. This is an actor-critic algorithm
    that generates data via a behavior policy, inserts N-step transitions into
    a replay buffer, and periodically updates the policy (and as a result the
    behavior) by sampling uniformly from this buffer.
    """

    def __init__(
        self,
        config: MAD3PGConfig,
        environment,
        environment_spec,
        networks: Optional[List[MAD3PGNetwork]] = None,
    ):
        """Initialize the agent.
        Args:
            config: Configuration for the agent.
        """
        self._config = config
        self._accelerator = config.accelerator

        if not self._accelerator:
            self._accelerator = get_first_available_accelerator_type(['TPU', 'GPU', 'CPU'])

        if networks is None:
            online_networks = make_default_MAD3PGNetworks(
                action_spec=environment_spec.edge_action,
            )
        else:
            online_networks = networks

        self._environment = environment
        self._environment_spec = environment_spec
        # Target networks are just a copy of the online networks.
        target_networks = [copy.deepcopy(network) for network in online_networks]

        # Initialize the networks.
        for online_network in online_networks:
            online_network.init(environment_spec)
        for target_network in target_networks:
            target_network.init(environment_spec)
        # for online_network, target_network in zip(online_networks, target_networks):
        #     online_network.init(self._environment_spec)
        #     target_network.init(self._environment_spec)

        # Create the behavior policy.
        policy_networks = [online_network.make_policy(self._environment_spec, self._config.sigma) for online_network in online_networks]

        # Create the replay server and grab its address.
        replay_tables = self.make_replay_tables(self._environment_spec)
        replay_server = reverb.Server(replay_tables, port=None)
        replay_client = reverb.Client(f'localhost:{replay_server.port}')

        # Create actor, dataset, and learner for generating, storing, and consuming
        # data respectively.
        adder = self.make_adder(replay_client=replay_client)
        actor = self.make_actor(
            policy_networks=policy_networks, 
            adder=adder,
        )
        
        dataset = self.make_dataset_iterator(replay_client=replay_client)
        learner = self.make_learner(
            online_networks=online_networks,
            target_networks=target_networks,
            dataset=dataset,
            counter=self._config.counter,
            logger=self._config.logger,
            checkpoint=self._config.checkpoint,
        )

        super().__init__(
            actor=actor,
            learner=learner,
            min_observations=max(self._config.batch_size, self._config.min_replay_size),
            observations_per_step=float(self._config.batch_size) / self._config.samples_per_insert)

        # Save the replay so we don't garbage collect it.
        self._replay_server = replay_server

    def make_replay_tables(
        self,
        environment_spec,
    ) -> List[reverb.Table]:
        """Create tables to insert data into."""
        if self._config.samples_per_insert is None:
            # We will take a samples_per_insert ratio of None to mean that there is
            # no limit, i.e. this only implies a min size limit.
            limiter = reverb.rate_limiters.MinSize(self._config.min_replay_size)

        else:
            # Create enough of an error buffer to give a 10% tolerance in rate.
            samples_per_insert_tolerance = 0.1 * self._config.samples_per_insert
            error_buffer = self._config.min_replay_size * samples_per_insert_tolerance
            limiter = reverb.rate_limiters.SampleToInsertRatio(
                min_size_to_sample=self._config.min_replay_size,
                samples_per_insert=self._config.samples_per_insert,
                error_buffer=error_buffer)

        replay_table = reverb.Table(
            name=self._config.replay_table_name,
            sampler=reverb.selectors.Uniform(),
            remover=reverb.selectors.Fifo(),
            max_size=self._config.max_replay_size,
            rate_limiter=limiter,
            signature=reverb_adders.NStepTransitionAdder.signature(
                environment_spec))

        return [replay_table]

    def make_dataset_iterator(
        self,
        replay_client: reverb.Client,
    ) -> Iterator[reverb.ReplaySample]:
        """Create a dataset iterator to use for learning/updating the agent."""
        # The dataset provides an interface to sample from replay.
        dataset = datasets.make_reverb_dataset(
            table=self._config.replay_table_name,
            server_address=replay_client.server_address,
            batch_size=self._config.batch_size,
            prefetch_size=self._config.prefetch_size)

        replicator = get_replicator(self._config.accelerator)
        dataset = replicator.experimental_distribute_dataset(dataset)

        # TODO: Fix type stubs and remove.
        return iter(dataset)  # pytype: disable=wrong-arg-types

    def make_adder(
        self,
        replay_client: reverb.Client,
    ) -> adders.Adder: 
        """Create an adder which records data generated by the actor/environment."""
        return reverb_adders.NStepTransitionAdder(
            priority_fns={self._config.replay_table_name: lambda x: 1.},
            client=replay_client,
            n_step=self._config.n_step,
            discount=self._config.discount)

    def make_actor(
        self,
        policy_networks: List[snt.Module],
        adder: Optional[adders.Adder] = None,
        variable_source: Optional[core.VariableSource] = None,
    ):
        """Create an actor instance."""
        if variable_source:
            # Create the variable client responsible for keeping the actor up-to-date.
            variables = dict()
            for i in range(len(policy_networks)):
                variables['edge_' + str(i)] = policy_networks[i].variables
            variable_client = variable_utils.VariableClient(
                client=variable_source,
                variables=variables,
                update_period=1000,
            )

            # Make sure not to use a random policy after checkpoint restoration by
            # assigning variables before running the environment loop.
            variable_client.update_and_wait()

        else:
            variable_client = None

        # Create the actor which defines how we take actions.
        return actors.FeedForwardActor(
            policy_networks=policy_networks,
            edge_number=self._environment._config.edge_number,
            edge_action_size=self._environment._action_size,
            adder=adder,
            variable_client=variable_client,
        )

    def make_learner(
        self,
        online_networks: List[MAD3PGNetwork], 
        target_networks: List[MAD3PGNetwork],
        dataset: Iterator[reverb.ReplaySample],
        counter: Optional[counting.Counter] = None,
        logger: Optional[loggers.Logger] = None,
        checkpoint: bool = False,
    ):
        """Creates an instance of the learner."""
        # The learner updates the parameters (and initializes them).
        return learning.MAD3PGLearner(
            policy_networks=[online_network.policy_network for online_network in online_networks],
            critic_networks=[online_network.critic_network for online_network in online_networks],

            target_policy_networks=[target_network.policy_network for target_network in target_networks],
            target_critic_networks=[target_network.critic_network for target_network in target_networks],
            
            discount=self._config.discount,
            target_update_period=self._config.target_update_period,
            dataset_iterator=dataset,

            observation_networks=[online_network.observation_network for online_network in online_networks],
            target_observation_networks=[target_network.observation_network for target_network in target_networks],

            policy_optimizers=self._config.policy_optimizers,
            critic_optimizers=self._config.critic_optimizers,

            clipping=self._config.clipping,
            replicator=get_replicator(self._config.accelerator),

            counter=counter,
            logger=logger,
            checkpoint=checkpoint,

            edge_number=self._environment._config.edge_number,
            edge_action_size=self._environment._action_size,
        )


class MultiAgentDistributedDDPG:
    """Program definition for MAD4PG."""
    def __init__(
        self,
        config: MAD3PGConfig,
        environment_factory: Callable[[bool], dm_env.Environment],
        environment_spec,
        networks: Optional[List[MAD3PGNetwork]] = None,
        num_actors: int = 1,
        num_caches: int = 0,
        max_actor_steps: Optional[int] = None,
        log_every: float = 30.0,
    ):
        """Initialize the MAD3PG agent."""
        self._config = config

        self._accelerator = config.accelerator
        if self._accelerator is not None and self._accelerator not in _ACCELERATORS:
            raise ValueError(f'Accelerator must be one of {_ACCELERATORS}, '
                            f'not "{self._accelerator}".')

        self._num_actors = num_actors
        self._num_caches = num_caches
        self._max_actor_steps = max_actor_steps
        self._log_every = log_every
        self._networks = networks
        self._environment_spec = environment_spec
        self._environment_factory = environment_factory
        # Create the agent.
        self._agent = MAD3PGAgent(
            config=self._config,
            environment=self._environment_factory(False),
            environment_spec=self._environment_spec,
            networks=self._networks,
        )

    def replay(self):
        """The replay storage."""
        return self._agent.make_replay_tables(self._environment_spec)

    def counter(self):
        return tf2_savers.CheckpointingRunner(counting.Counter(),
                                            time_delta_minutes=30,
                                            subdirectory='counter')

    def coordinator(self, counter: counting.Counter):
        return lp_utils.StepsLimiter(counter, self._max_actor_steps)

    def learner(
        self,
        replay: reverb.Client,
        counter: counting.Counter,
    ):
        """The Learning part of the agent."""
        
        # If we are running on multiple accelerator devices, this replicates
        # weights and updates across devices.
        replicator = get_replicator(self._accelerator)

        with replicator.scope():
            # Create the networks to optimize (online) and target networks.
            online_networks = self._networks
            target_networks = [copy.deepcopy(network) for network in online_networks]

            # Initialize the networks.
            for online_network, target_network in zip(online_networks, target_networks):
                online_network.init(self._environment_spec)
                target_network.init(self._environment_spec)

        dataset = self._agent.make_dataset_iterator(replay)
        counter = counting.Counter(counter, 'learner')
        logger = loggers.make_default_logger(
            'learner', time_delta=self._log_every, steps_key='learner_steps')

        return self._agent.make_learner(
            online_networks=online_networks, 
            target_networks=target_networks,
            dataset=dataset,
            counter=counter,
            logger=logger,
            checkpoint=True,
        )

    def actor(
        self,
        replay: reverb.Client,
        variable_source: acme.VariableSource,
        counter: counting.Counter,
    ) -> acme.EnvironmentLoop:
        """The actor process."""

        # Create the behavior policy.        
        networks = self._networks
        
        for network in networks:
            network.init(self._environment_spec)

        policy_networks = [
            network.make_policy(environment_spec=self._environment_spec, sigma=self._config.sigma)
            for network in networks
        ]
        
        # Create the environment
        environment = self._environment_factory(False)

        # Create the agent.
        actor = self._agent.make_actor(
            policy_networks=policy_networks,
            adder=self._agent.make_adder(replay),
            variable_source=variable_source,
        )

        # Create logger and counter; actors will not spam bigtable.
        counter = counting.Counter(counter, 'actor')
        logger = loggers.make_default_logger(
            'actor',
            save_data=False,
            time_delta=self._log_every,
            steps_key='actor_steps')

        # Create the loop to connect environment and agent.
        return acme.EnvironmentLoop(
            environment=environment, 
            actor=actor, 
            counter=counter, 
            logger=logger,
            label='Actor_Loop',    
        )

    def evaluator(
        self,
        variable_source: acme.VariableSource,
        counter: counting.Counter,
        logger: Optional[loggers.Logger] = None,
    ):
        """The evaluation process."""

        # Create the behavior policy.
        networks = self._networks
        for network in networks:
            network.init(self._environment_spec)
        
        policy_networks = [
            network.make_policy(self._environment_spec) for network in networks
        ]
        
        # Make the environment
        environment = self._environment_factory(True)

        # Create the agent.
        actor = self._agent.make_actor(
            policy_networks=policy_networks,
            variable_source=variable_source,
        )

        # Create logger and counter.
        counter = counting.Counter(counter, 'evaluator')
        logger = logger or loggers.make_default_logger(
            'evaluator',
            time_delta=self._log_every,
            steps_key='evaluator_steps',
        )

        # Create the run loop and return it.
        return acme.EnvironmentLoop(
            environment=environment, 
            actor=actor, 
            counter=counter, 
            logger=logger,
            label='Evaluator_Loop',
        )

    def build(self, name='mad4pg'):
        """Build the distributed agent topology."""
        program = lp.Program(name=name)

        with program.group('replay'):
            replay = program.add_node(lp.ReverbNode(self.replay))

        with program.group('counter'):
            counter = program.add_node(lp.CourierNode(self.counter))

        if self._max_actor_steps:
            with program.group('coordinator'):
                _ = program.add_node(lp.CourierNode(self.coordinator, counter))

        with program.group('learner'):
            learner = program.add_node(lp.CourierNode(self.learner, replay, counter))

        with program.group('evaluator'):
            program.add_node(lp.CourierNode(self.evaluator, learner, counter))

        if not self._num_caches:
            # Use our learner as a single variable source.
            sources = [learner]
        else:
            with program.group('cacher'):
                # Create a set of learner caches.
                sources = []
                for _ in range(self._num_caches):
                    cacher = program.add_node(
                        lp.CacherNode(
                            learner, refresh_interval_ms=2000, stale_after_ms=4000))
                sources.append(cacher)

        with program.group('actor'):
            # Add actors which pull round-robin from our variable sources.
            for actor_id in range(self._num_actors):
                source = sources[actor_id % len(sources)]
                program.add_node(lp.CourierNode(self.actor, replay, source, counter))

        return program


def ensure_accelerator(accelerator: str) -> str:
    """Checks for the existence of the expected accelerator type.
    Args:
        accelerator: 'CPU', 'GPU' or 'TPU'.
    Returns:
        The validated `accelerator` argument.
    Raises:
        RuntimeError: Thrown if the expected accelerator isn't found.
    """
    devices = tf.config.get_visible_devices(device_type=accelerator)

    if devices:
        return accelerator
    else:
        error_messages = [f'Couldn\'t find any {accelerator} devices.',
                        'tf.config.get_visible_devices() returned:']
        error_messages.extend([str(d) for d in devices])
        raise RuntimeError('\n'.join(error_messages))


def get_first_available_accelerator_type(
    wishlist: Sequence[str] = ('TPU', 'GPU', 'CPU')) -> str:
    """Returns the first available accelerator type listed in a wishlist.
    Args:
        wishlist: A sequence of elements from {'CPU', 'GPU', 'TPU'}, listed in
        order of descending preference.
    Returns:
        The first available accelerator type from `wishlist`.
    Raises:
        RuntimeError: Thrown if no accelerators from the `wishlist` are found.
    """
    get_visible_devices = tf.config.get_visible_devices

    for wishlist_device in wishlist:
        devices = get_visible_devices(device_type=wishlist_device)
        if devices:
            return wishlist_device

    available = ', '.join(
        sorted(frozenset([d.type for d in get_visible_devices()])))
    raise RuntimeError(
        'Couldn\'t find any devices from {wishlist}.' +
        f'Only the following types are available: {available}.')


# Only instantiate one replicator per (process, accelerator type), in case
# a replicator stores state that needs to be carried between its method calls.
@functools.lru_cache()
def get_replicator(accelerator: Optional[str]) -> Replicator:
    """Returns a replicator instance appropriate for the given accelerator.
    This caches the instance using functools.cache, so that only one replicator
    is instantiated per process and argument value.
    Args:
        accelerator: None, 'TPU', 'GPU', or 'CPU'. If None, the first available
        accelerator type will be chosen from ('TPU', 'GPU', 'CPU').
    Returns:
        A replicator, for replciating weights, datasets, and updates across
        one or more accelerators.
    """
    if accelerator:
        accelerator = ensure_accelerator(accelerator)
    else:
        accelerator = get_first_available_accelerator_type()

    if accelerator == 'TPU':
        tf.tpu.experimental.initialize_tpu_system()
        return snt.distribute.TpuReplicator()
    else:
        return snt.distribute.Replicator()
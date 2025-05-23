import tensorflow as tf
import typing as T
from tensorflow import keras
try:
    import tensorflow_privacy as tf_privacy
    __tf_privacy_available = True
except ModuleNotFoundError:
    __tf_privacy_available = False

import logging

import tsgm


logger = logging.getLogger('models')
logger.setLevel(logging.DEBUG)


def _is_dp_optimizer(optimizer: keras.optimizers.Optimizer) -> bool:
    return __tf_privacy_available \
        and (isinstance(optimizer, tf_privacy.DPKerasAdagradOptimizer)
             or isinstance(optimizer, tf_privacy.DPKerasAdamOptimizer)
             or isinstance(optimizer, tf_privacy.DPKerasSGDOptimizer))


class GAN(keras.Model):
    """
    GAN implementation for unlabeled time series.
    """
    def __init__(self, discriminator: keras.Model, generator: keras.Model, latent_dim: int, use_wgan: bool = False) -> None:
        """
        :param discriminator: A discriminator model which takes a time series as input and check
            whether the sample is real or fake.
        :type discriminator: keras.Model
        :param generator: Takes as input a random noise vector of `latent_dim` length and returns
            a simulated time-series.
        :type generator: keras.Model
        :param latent_dim: The size of the noise vector.
        :type latent_dim: int
        :param use_wgan: Use Wasserstein GAN with gradien penalty
        :type use_wgan: bool
        """
        super(GAN, self).__init__()
        self.discriminator = discriminator
        self.generator = generator
        self.latent_dim = latent_dim
        self._seq_len = self.generator.output_shape[1]
        self.use_wgan = use_wgan
        self.gp_weight = 10.0

        self.gen_loss_tracker = keras.metrics.Mean(name="generator_loss")
        self.disc_loss_tracker = keras.metrics.Mean(name="discriminator_loss")

    def wgan_discriminator_loss(self, real_sample, fake_sample):
        real_loss = tf.reduce_mean(real_sample)
        fake_loss = tf.reduce_mean(fake_sample)
        return fake_loss - real_loss

    # Define the loss functions to be used for generator
    def wgan_generator_loss(self, fake_sample):
        return -tf.reduce_mean(fake_sample)

    def gradient_penalty(self, batch_size, real_samples, fake_samples):
        # get the interpolated samples
        alpha = tf.random.normal([batch_size, 1, 1], 0.0, 1.0)
        diff = fake_samples - real_samples
        interpolated = real_samples + alpha * diff
        with tf.GradientTape() as gp_tape:
            gp_tape.watch(interpolated)
            # 1. Get the discriminator output for this interpolated sample.
            pred = self.discriminator(interpolated, training=True)

        # 2. Calculate the gradients w.r.t to this interpolated sample.
        grads = gp_tape.gradient(pred, [interpolated])[0]
        # 3. Calcuate the norm of the gradients
        norm = tf.sqrt(tf.reduce_sum(tf.square(grads), axis=[1, 2]))
        gp = tf.reduce_mean((norm - 1.0) ** 2)
        return gp

    @property
    def metrics(self) -> T.List:
        """
        :returns: A list of metrics trackers (e.g., generator's loss and discriminator's loss).
        """
        return [self.gen_loss_tracker, self.disc_loss_tracker]

    def compile(self, d_optimizer: keras.optimizers.Optimizer, g_optimizer: keras.optimizers.Optimizer,
                loss_fn: keras.losses.Loss) -> None:
        """
        Compiles the generator and discriminator models.

        :param d_optimizer: An optimizer for the GAN's discriminator.
        :type d_optimizer: keras.Model
        :param g_optimizer: An optimizer for the GAN's generator.
        :type generator: keras.Model
        :param loss_fn: Loss function.
        :type loss_fn: keras.losses.Loss
        """
        super(GAN, self).compile()
        self.d_optimizer = d_optimizer
        self.g_optimizer = g_optimizer
        self.loss_fn = loss_fn

        generator_dp = _is_dp_optimizer(d_optimizer)
        discriminator_dp = _is_dp_optimizer(g_optimizer)

        if generator_dp != discriminator_dp:
            logger.warning(f"One of the optimizers is DP and another one is not. generator_dp={generator_dp}, discriminator_dp={discriminator_dp}")

        self.dp = generator_dp and discriminator_dp

    def _get_random_vector_labels(self, batch_size: int, labels=None) -> tsgm.types.Tensor:
        return tf.random.normal(shape=(batch_size, self.latent_dim))

    def train_step(self, data: tsgm.types.Tensor) -> T.Dict[str, float]:
        """
        Performs a training step using a batch of data, stored in data.

        :param data: A batch of data in a format batch_size x seq_len x feat_dim
        :type data: tsgm.types.Tensor

        :returns: A dictionary with generator (key "g_loss") and discriminator (key "d_loss") losses
        :rtype: T.Dict[str, float]
        """
        real_data = data
        batch_size = tf.shape(real_data)[0]
        # Generate ts
        random_vector = self._get_random_vector_labels(batch_size)
        fake_data = self.generator(random_vector)

        combined_data = tf.concat(
            [fake_data, real_data], axis=0
        )

        # Labels for descriminator
        # 1 == real data
        # 0 == fake data
        desc_labels = tf.concat(
            [tf.ones((batch_size, 1)), tf.zeros((batch_size, 1))], axis=0
        )
        with tf.GradientTape() as tape:
            predictions = self.discriminator(combined_data)
            if self.use_wgan:
                fake_logits = self.discriminator(fake_data, training=True)
                # Get the logits for the real samples
                real_logits = self.discriminator(real_data, training=True)

                # Calculate the discriminator loss using the fake and real sample logits
                d_cost = self.wgan_discriminator_loss(real_logits, fake_logits)
                # Calculate the gradient penalty
                gp = self.gradient_penalty(batch_size, real_data, fake_data)
                # Add the gradient penalty to the original discriminator loss
                d_loss = d_cost + gp * self.gp_weight
            else:
                d_loss = self.loss_fn(desc_labels, predictions)
        grads = tape.gradient(d_loss, self.discriminator.trainable_weights)
        self.d_optimizer.apply_gradients(
            zip(grads, self.discriminator.trainable_weights)
        )

        random_vector = self._get_random_vector_labels(batch_size=batch_size)

        # Pretend that all samples are real
        misleading_labels = tf.zeros((batch_size, 1))

        # Train generator (with updating the discriminator)
        with tf.GradientTape() as tape:
            fake_data = self.generator(random_vector)
            predictions = self.discriminator(fake_data)
            if self.use_wgan:
                # uses logits
                g_loss = self.wgan_generator_loss(predictions)
            else:
                g_loss = self.loss_fn(misleading_labels, predictions)

        grads = tape.gradient(g_loss, self.generator.trainable_weights)
        self.g_optimizer.apply_gradients(zip(grads, self.generator.trainable_weights))

        self.gen_loss_tracker.update_state(g_loss)
        self.disc_loss_tracker.update_state(d_loss)
        return {
            "g_loss": self.gen_loss_tracker.result(),
            "d_loss": self.disc_loss_tracker.result(),
        }

    def generate(self, num: int) -> tsgm.types.Tensor:
        """
        Generates new data from the model.

        :param num: the number of samples to be generated.
        :type num: int

        :returns: Generated samples
        :rtype: tsgm.types.Tensor
        """
        random_vector_labels = self._get_random_vector_labels(batch_size=num)
        return self.generator(random_vector_labels)

    def clone(self) -> "GAN":
        """
        Clones GAN object

        :returns: The exact copy of the object
        :rtype: "GAN"
        """
        copy_model = GAN(self.discriminator, self.generator, latent_dim=self.latent_dim)
        copy_model = copy_model.set_weights(self.get_weights())
        return copy_model


class ConditionalGAN(keras.Model):
    """
    Conditional GAN implementation for labeled and temporally labeled time series.
    """
    def __init__(self, discriminator: keras.Model, generator: keras.Model, latent_dim: int, temporal=False, use_wgan=False) -> None:
        """
        :param discriminator: A discriminator model which takes a time series as input and check
            whether the sample is real or fake.
        :type discriminator: keras.Model
        :param generator: Takes as input a random noise vector of `latent_dim` length and return
            a simulated time-series.
        :type generator: keras.Model
        :param latent_dim: The size of the noise vector.
        :type latent_dim: int
        :param temporal: Indicates whether the time series temporally labeled or not.
        :type temporal: bool
        """
        super(ConditionalGAN, self).__init__()
        self.discriminator = discriminator
        self.generator = generator
        self.latent_dim = latent_dim
        self._seq_len = self.generator.output_shape[1]

        self.gen_loss_tracker = keras.metrics.Mean(name="generator_loss")
        self.disc_loss_tracker = keras.metrics.Mean(name="discriminator_loss")
        self._temporal = temporal

    @property
    def metrics(self) -> T.List:
        """
        :returns: A list of metrics trackers (e.g., generator's loss and discriminator's loss).
        :rtype: T.List
        """
        return [self.gen_loss_tracker, self.disc_loss_tracker]

    def compile(self, d_optimizer: keras.optimizers.Optimizer, g_optimizer: keras.optimizers.Optimizer, loss_fn: T.Callable) -> None:
        """
        Compiles the generator and discriminator models.

        :param d_optimizer: An optimizer for the GAN's discriminator.
        :type d_optimizer: keras.Model
        :param g_optimizer: An optimizer for the GAN's generator.
        :type generator: keras.Model
        :param loss_fn: Loss function.
        :type loss_fn: keras.losses.Loss
        """
        # TODO: move `.compile logic to a base GAN class
        super(ConditionalGAN, self).compile()
        self.d_optimizer = d_optimizer
        self.g_optimizer = g_optimizer
        self.loss_fn = loss_fn

        generator_dp = _is_dp_optimizer(d_optimizer)
        discriminator_dp = _is_dp_optimizer(g_optimizer)

        if generator_dp != discriminator_dp:
            logger.warning(f"One of the optimizers is DP and another one is not. generator_dp={generator_dp}, discriminator_dp={discriminator_dp}")

        self.dp = generator_dp and discriminator_dp

    def _get_random_vector_labels(self, batch_size: int, labels: tsgm.types.Tensor) -> None:
        if self._temporal:
            random_latent_vectors = tf.random.normal(shape=(batch_size, self._seq_len, self.latent_dim))
            random_vector_labels = tf.concat(
                [random_latent_vectors, labels[:, :, None]], axis=2
            )
        else:
            random_latent_vectors = tf.random.normal(shape=(batch_size, self.latent_dim))
            random_vector_labels = tf.concat(
                [random_latent_vectors, labels], axis=1
            )
        return random_vector_labels

    def _get_output_shape(self, labels: tsgm.types.Tensor) -> int:
        if self._temporal:
            if len(labels.shape) == 2:
                return 1
            else:
                return labels.shape[2]
        else:
            return labels.shape[1]

    def train_step(self, data: T.Tuple) -> T.Dict[str, float]:
        """
        Performs a training step using a batch of data, stored in data.

        :param data: A batch of data in a format batch_size x seq_len x feat_dim
        :type data: tsgm.types.Tensor

        :returns: A dictionary with generator (key "g_loss") and discriminator (key "d_loss") losses
        :rtype: T.Dict[str, float]
        """
        real_ts, labels = data
        output_dim = self._get_output_shape(labels)
        batch_size = tf.shape(real_ts)[0]
        if not self._temporal:
            rep_labels = labels[:, :, None]
            rep_labels = tf.repeat(
                rep_labels, repeats=[self._seq_len]
            )
        else:
            rep_labels = labels

        rep_labels = tf.reshape(
            rep_labels, (-1, self._seq_len, output_dim)
        )

        # Generate ts
        random_vector_labels = self._get_random_vector_labels(batch_size=batch_size, labels=labels)
        generated_ts = self.generator(random_vector_labels)

        fake_data = tf.concat([generated_ts, rep_labels], -1)
        real_data = tf.concat([real_ts, rep_labels], -1)
        combined_data = tf.concat(
            [fake_data, real_data], axis=0
        )

        # Labels for descriminator
        # 1 == real data
        # 0 == fake data
        desc_labels = tf.concat(
            [tf.ones((batch_size, 1)), tf.zeros((batch_size, 1))], axis=0
        )

        with tf.GradientTape() as tape:
            predictions = self.discriminator(combined_data)
            d_loss = self.loss_fn(desc_labels, predictions)

        if self.dp:
            # For DP optimizers from `tensorflow.privacy`
            self.d_optimizer.minimize(d_loss, self.discriminator.trainable_weights, tape=tape)
        else:
            grads = tape.gradient(d_loss, self.discriminator.trainable_weights)

            self.d_optimizer.apply_gradients(
                zip(grads, self.discriminator.trainable_weights)
            )

        random_vector_labels = self._get_random_vector_labels(batch_size=batch_size, labels=labels)

        # Pretend that all samples are real
        misleading_labels = tf.zeros((batch_size, 1))

        # Train generator (with updating the discriminator)
        with tf.GradientTape() as tape:
            fake_samples = self.generator(random_vector_labels)
            fake_data = tf.concat([fake_samples, rep_labels], -1)
            predictions = self.discriminator(fake_data)
            g_loss = self.loss_fn(misleading_labels, predictions)

        if self.dp:
            # For DP optimizers from `tensorflow.privacy`
            self.g_optimizer.minimize(g_loss, self.generator.trainable_weights, tape=tape)
        else:
            grads = tape.gradient(g_loss, self.generator.trainable_weights)
            self.g_optimizer.apply_gradients(zip(grads, self.generator.trainable_weights))

        self.gen_loss_tracker.update_state(g_loss)
        self.disc_loss_tracker.update_state(d_loss)
        return {
            "g_loss": self.gen_loss_tracker.result(),
            "d_loss": self.disc_loss_tracker.result(),
        }

    def generate(self, labels: tsgm.types.Tensor) -> tsgm.types.Tensor:
        """
        Generates new data from the model.

        :param labels: the number of samples to be generated.
        :type labels: tsgm.types.Tensor

        :returns: generated samples
        :rtype: tsgm.types.Tensor
        """
        batch_size = labels.shape[0]

        random_vector_labels = self._get_random_vector_labels(
            batch_size=batch_size, labels=labels)
        return self.generator(random_vector_labels)

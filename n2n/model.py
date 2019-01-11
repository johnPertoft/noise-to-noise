import tensorflow as tf

from .nets import rednet
from .nets import unet


tf.app.flags.DEFINE_string('architecture', 'unet', 'The network architecture to use.')
tf.app.flags.DEFINE_string('loss', 'l2', 'The loss function to use.')
tf.app.flags.DEFINE_float('learning_rate', 1e-4, 'The learning rate.')
tf.app.flags.DEFINE_boolean('variable_histograms', False, 'Whether to add histogram summaries for model variables.')
tf.app.flags.DEFINE_boolean('gradient_histograms', False, 'Whether to add histogram summaries for model gradients.')

FLAGS = tf.app.flags.FLAGS


# TODO: Add PSNR metric.
# TODO: Paper mentions rampdown period but no details. Check reference implementation.
# TODO: Add a comparison of some simple image processing approach. I.e. median of neighborhood or similar.
# TODO: Add summary of average metric of noise img vs ground truth as well for comparison.


def model_fn(features, labels, mode, config):
    assert labels is None, '`labels` argument should not be used.'

    is_training = mode == tf.estimator.ModeKeys.TRAIN
    global_step = tf.train.get_or_create_global_step()

    if FLAGS.architecture == 'unet':
        architecture = unet.model_fn
    elif FLAGS.architecture == 'rednet':
        architecture = rednet.model_fn
    else:
        raise ValueError(f'Invalid architecture: `{FLAGS.architecture}`.')

    if FLAGS.loss == 'l0':
        def l0_loss(labels, predictions):
            max_steps = 200_000
            ratio = tf.math.minimum(global_step, max_steps) / max_steps
            gamma = 2 * (1 - ratio)
            gamma = tf.cast(gamma, tf.float32)
            loss = (tf.abs(labels - predictions) + 1e-8) ** gamma
            loss = tf.reduce_mean(loss)
            return loss
        loss_fn = l0_loss
    elif FLAGS.loss == 'l1':
        loss_fn = tf.losses.absolute_difference
    elif FLAGS.loss == 'l2':
        loss_fn = tf.losses.mean_squared_error
    else:
        raise ValueError(f'Invalid loss: `{FLAGS.loss}`.')

    denoise = tf.make_template('denoise', architecture, is_training=is_training, output_fn=tf.nn.sigmoid)

    x_hat = denoise(features['input'])

    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=x_hat,
            export_outputs={'denoised': x_hat})

    loss = loss_fn(x_hat, features['target'])

    mean_ground_truth_loss = tf.metrics.mean(loss_fn(x_hat, features['gt']))

    if mode == tf.estimator.ModeKeys.EVAL:
        tf.summary.image('denoising', tf.concat((features['input'], x_hat, features['gt']), axis=2))

        crop_central_fraction = 0.4
        crop = tf.concat((
            tf.image.central_crop(features['input'], crop_central_fraction),
            tf.image.central_crop(x_hat, crop_central_fraction),
            tf.image.central_crop(features['gt'], crop_central_fraction)),
            axis=2)
        _, ch, cw, _ = crop.shape.as_list()
        crop = tf.image.resize_images(crop, (int(ch * 1.5), int(cw * 1.5)))
        tf.summary.image('denoising_zoomed', crop)

        eval_summary_hook = tf.train.SummarySaverHook(
            save_steps=1,
            output_dir=f'{config.model_dir}/eval',
            summary_op=tf.summary.merge_all())

        return tf.estimator.EstimatorSpec(
            mode=mode,
            loss=loss,
            eval_metric_ops={'ground_truth_loss': mean_ground_truth_loss},
            evaluation_hooks=[eval_summary_hook])

    if mode == tf.estimator.ModeKeys.TRAIN:
        learning_rate = FLAGS.learning_rate
        tf.summary.scalar('learning_rate', learning_rate)

        optimizer = tf.train.AdamOptimizer(
            learning_rate=learning_rate,
            beta1=0.9,
            beta2=0.999)
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            grads_and_vars = optimizer.compute_gradients(loss)
            train_op = optimizer.apply_gradients(grads_and_vars, global_step)

        for g, v in grads_and_vars:
            if FLAGS.variable_histograms:
                tf.summary.histogram(v.op.name, v)
            if FLAGS.gradient_histograms:
                tf.summary.histogram(g.op.name, g)

        return tf.estimator.EstimatorSpec(
            mode=mode,
            loss=loss,
            train_op=train_op)

import os
import sys
import time
import logging
import argparse
import numpy as np
import tensorflow as tf
from dispnet import DispNet
from util import init_logger, trainingLists_conf,get_var_to_restore_list
from tensorflow.python.client import timeline

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--training", dest="training", required=True, type=str,metavar="FILE", help='path to the training list file')
    parser.add_argument("--testing",dest='testing',required=True,type=str,metavar='FILE',help="path to the test list file")
    parser.add_argument("-c", "--ckpt", dest="checkpoint_path", default=".", type=str, help='model checkpoint path')
    parser.add_argument("-b", "--batch_size", dest="batch_size", default=4, type=int,help='batch size')
    parser.add_argument("-l", "--log_step", dest="log_step", type=int, default=100,help='log step size')
    parser.add_argument("-w", "--weights", dest="weights", help="preinitialization weights", metavar="FILE",default=None)
    parser.add_argument("-s", "--save_step", dest="save_step", type=int, default=1000,help='save checkpoint step size')
    parser.add_argument("-n", "--n_steps", dest="n_steps", type=int, default=500000,help='number of training steps')
    parser.add_argument("--corr_type", dest="corr_type", type=str, default="tf",help="correlation layer realization",choices=['tf','cuda','none'])
    parser.add_argument("-th","--confidence_th",dest="confidence_th",type=int, default="0", help="threshold to be applied on the confidence to mask out values")
    parser.add_argument("--smooth",type=float,default=0,help="smoothness lambda to be used for l1 regularization")

    args = parser.parse_args()
    
    dataset = trainingLists_conf(args.training,args.testing)

    tf.logging.set_verbosity(tf.logging.ERROR)
    is_corr = args.corr_type!='none'
    dispnet = DispNet(mode="traintest", ckpt_path=args.checkpoint_path, dataset=dataset,batch_size=args.batch_size, is_corr=is_corr, corr_type=args.corr_type,smoothness_lambda=args.smooth,confidence_th=args.confidence_th)


    if not os.path.exists(args.checkpoint_path):
        os.mkdir(args.checkpoint_path)
    model_name = "DispNet"
    if is_corr:
        model_name += "Corr1D"
    init_logger(args.checkpoint_path, name=model_name)
    writer = tf.summary.FileWriter(args.checkpoint_path)

    schedule_step = 50000
    # weights_schedule = [[0., 0., 0., 0., .2, 1.],
    #                     [0., 0., 0., .2, 1., .5],
    #                     [0., 0., .2, 1., .5, 0.],
    #                     [0., .2, 1., .5, 0., 0.],
    #                     [.2, 1., .5, 0., 0., 0.],
    #                     [1., .5, 0., 0., 0., 0.],
    #                     [1., 0., 0., 0., 0., 0.]]
    weights_schedule = [[1.,0.,0.,0.,0.,0.]]
    lr_schedule = [1e-4] * 5
    for i in range(20):
        lr_schedule.extend([(lr_schedule[-1] / 2.)] * 3)

    log_step = args.log_step
    save_step = args.save_step
    test_step = save_step
    N_test = 1000

    gpu_options = tf.GPUOptions(allow_growth=True)
#    options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
#    run_metadata = tf.RunMetadata()
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as sess:
        sess.run(dispnet.init)
        logging.debug("initialized\n")
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)
        logging.debug("queue runners started\n")
        try:
            l_mean = 0

            ckpt = tf.train.latest_checkpoint(args.checkpoint_path)
            if ckpt:
                logging.info("Restoring from %s\n" % ckpt)
                dispnet.saver.restore(sess=sess, save_path=ckpt)
                step = int(ckpt[len(os.path.join(args.checkpoint_path, model_name))+1:])
                logging.info("step: %d\n" % step)
            else:
                step = 0
                #restore preinitialization weights if present
                if args.weights is not None:
                    var_to_restore = get_var_to_restore_list(args.weights, [], prefix="model/")
                    print('Found {} variables to restore'.format(len(var_to_restore)))
                    restorer = tf.train.Saver(var_list=var_to_restore)
                    restorer.restore(sess, args.weights)
                    print('Weights restored')

            last_error = 1000
            while step < args.n_steps:
                schedule_current = min(step // schedule_step, len(weights_schedule) - 1)
                feed_dict = {}
                feed_dict[dispnet.loss_weights] = np.array(weights_schedule[schedule_current])
                feed_dict[dispnet.learning_rate] = lr_schedule[schedule_current]
                feed_dict[dispnet.test_error] = last_error
                if step % schedule_step == 0:
                    schedule_current = min(step // schedule_step, len(weights_schedule)-1)
                    feed_dict[dispnet.loss_weights] = np.array(weights_schedule[schedule_current])
                    feed_dict[dispnet.learning_rate] = lr_schedule[schedule_current]
                    logging.info("iter: %d, switching weights:" % step)
                    logging.info(str(feed_dict[dispnet.loss_weights])+'\n')
                    logging.info("learning rate: %f\n" % feed_dict[dispnet.learning_rate])
                
                start = time.time()
                _, l, err = sess.run([dispnet.train_step, dispnet.loss, dispnet.train_error],feed_dict=feed_dict)
                end = time.time()
                l_mean += l
                step += 1
                if step % test_step == 0:
                    test_err = 0
                    logging.info("Testing...\n")
                    for j in range(N_test):
                        err = sess.run([dispnet.test_error])
                        test_err += err[0]
                    test_err = test_err / float(N_test)
                    logging.info("Test error %f\n" % test_err)
                    last_error = test_err

                if step % log_step == 0:
                    l_mean = np.array(l_mean / float(log_step))
                    feed_dict[dispnet.mean_loss] = l_mean
                    s = sess.run(dispnet.merged_summary, feed_dict=feed_dict)
                    writer.add_summary(s, step)
                    logging.debug("iter: %d, f/b pass time: %f, loss: %f, error %f\n" %(step, ((end - start) / float(log_step)), l_mean, err))
                    l_mean = 0
                if step % save_step == 0:
                    logging.info("saving to file %s.\n" % (os.path.join(args.checkpoint_path, MODEL_NAME)))
                    dispnet.saver.save(sess, os.path.join(args.checkpoint_path, MODEL_NAME),global_step=step)

        except tf.errors.OutOfRangeError:
            logging.INFO('Done training for %d epochs, %d steps.\n' % (FLAGS.num_epochs, step))

        finally:
            coord.request_stop()
            coord.join(threads)
            sess.close()

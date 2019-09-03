import os
import sys
import shutil
import inspect
import datetime
import functools
import tensorflow as tf
import multiprocessing as mp
import lib.plot, lib.dataloader, lib.logger, lib.ops.LSTM
from lib.general_utils import Pool
from Model.RNATracker import RNATracker

tf.logging.set_verbosity(tf.logging.FATAL)
tf.app.flags.DEFINE_string('output_dir', '', '')
tf.app.flags.DEFINE_integer('epochs', 200, '')
tf.app.flags.DEFINE_integer('nb_gpus', 1, '')
tf.app.flags.DEFINE_bool('use_clr', True, '')
tf.app.flags.DEFINE_bool('use_momentum', False, '')
tf.app.flags.DEFINE_integer('parallel_processes', 1, '')
tf.app.flags.DEFINE_integer('units', 32, '')
tf.app.flags.DEFINE_bool('use_bn', False, '')
tf.app.flags.DEFINE_bool('return_label', False, '')
tf.app.flags.DEFINE_bool('use_embedding', False, '')
tf.app.flags.DEFINE_bool('use_structure', False, '')
tf.app.flags.DEFINE_string('merge_mode', 'concatenation', '')
tf.app.flags.DEFINE_bool('augment_features', False, '')
# switch dataset
tf.app.flags.DEFINE_bool('use_smaller_clip_seq', False, '')
FLAGS = tf.app.flags.FLAGS

if FLAGS.use_smaller_clip_seq:
    lib.dataloader.path_template = lib.dataloader.path_template.replace('30000', '5000')
BATCH_SIZE = 200  # 200 * FLAGS.nb_gpus if FLAGS.nb_gpus > 0 else 200
EPOCHS = FLAGS.epochs  # How many iterations to train for
DEVICES = ['/gpu:%d' % (i) for i in range(FLAGS.nb_gpus)] if FLAGS.nb_gpus > 0 else ['/cpu:0']
RBP_LIST = lib.dataloader.all_rbps
MAX_LEN = 101
N_EDGE_EMB = len(lib.dataloader.BOND_TYPE)

hp = {
    'learning_rate': 2e-4,
    'dropout_rate': 0.2,
    'use_clr': FLAGS.use_clr,
    'use_momentum': FLAGS.use_momentum,
    'units': FLAGS.units,
    'use_bn': FLAGS.use_bn,
    'augment_features': FLAGS.augment_features,
}


def Logger(q):
    logger = lib.logger.CSVLogger('rbp-results.csv', output_dir, ['RBP', 'acc', 'auc'])
    while True:
        msg = q.get()
        if msg == 'kill':
            logger.close()
            break
        logger.update_with_dict(msg)


def run_one_rbp(idx, q):
    rbp = RBP_LIST[idx]
    rbp_output = os.path.join(output_dir, rbp)
    os.makedirs(rbp_output)

    outfile = open(os.path.join(rbp_output, str(os.getpid())) + ".out", "w")
    sys.stdout = outfile
    sys.stderr = outfile
    print('training', RBP_LIST[idx])

    dataset = lib.dataloader.load_clip_seq([rbp], use_embedding=FLAGS.use_embedding,
                                           merge_seq_and_struct=FLAGS.use_structure,
                                           merge_mode=FLAGS.merge_mode)[0]  # load one at a time
    hp['features_dim'] = dataset['train_features'].shape[-1]
    model = RNATracker(MAX_LEN, dataset['VOCAB_VEC'].shape[1], len(lib.dataloader.BOND_TYPE),
                       dataset['VOCAB_VEC'], DEVICES, FLAGS.return_label, **hp)
    model.fit((dataset['train_seq'], dataset['train_features']), dataset['train_label'],
              EPOCHS, BATCH_SIZE, rbp_output, logging=True)
    all_prediction, acc, auc = model.predict((dataset['test_seq'], dataset['test_features']),
                                             BATCH_SIZE, y=dataset['test_label'])
    print('Evaluation on held-out test set, acc: %.3f, auc: %.3f' % (acc, auc))
    model.delete()
    del dataset
    lib.plot.reset()
    q.put({
        'RBP': rbp,
        'acc': acc,
        'auc': auc
    })


if __name__ == "__main__":

    cur_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if FLAGS.output_dir == '':
        output_dir = os.path.join('output', 'RNATracker', cur_time)
    else:
        output_dir = os.path.join('output', 'RNATracker', cur_time + '-' + FLAGS.output_dir)

    os.makedirs(output_dir)
    lib.plot.set_output_dir(output_dir)

    # backup python scripts, for future reference
    backup_dir = os.path.join(output_dir, 'backup/')
    os.makedirs(backup_dir)
    shutil.copy(__file__, backup_dir)
    shutil.copy(inspect.getfile(RNATracker), backup_dir)
    shutil.copy(inspect.getfile(lib.ops.LSTM), backup_dir)
    shutil.copy(inspect.getfile(lib.dataloader), backup_dir)

    manager = mp.Manager()
    q = manager.Queue()
    pool = Pool(FLAGS.parallel_processes + 1)
    logger_thread = pool.apply_async(Logger, (q,))
    pool.map(functools.partial(run_one_rbp, q=q), list(range(len(RBP_LIST))), chunksize=1)

    q.put('kill')  # terminate logger thread
    pool.close()
    pool.join()
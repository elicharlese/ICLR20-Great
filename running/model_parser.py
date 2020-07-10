import tensorflow as tf

from models import great_transformer, ggnn, rnn

class VarMisuseModel(tf.keras.layers.Layer):
	def __init__(self, config, vocab_dim):
		super(VarMisuseModel, self).__init__()
		
		# These layers are always used; initialize with any given model's hidden_dim
		random_init = tf.random_normal_initializer(stddev=config["transformer"]["hidden_dim"] ** -0.5)
		self.embed = tf.Variable(random_init([vocab_dim, config["transformer"]["hidden_dim"]]), dtype=tf.float32)
		self.prediction = tf.keras.layers.Dense(2) # Two pointers: bug location and repair
		
		# Next, parse the main 'model' from the config
		desc = config['training']['model'].split(' ')
		self.stack = []
		for kind in desc:
			if kind == 'great':
				self.stack.append(great_transformer.Transformer(config["transformer"], shared_embedding=self.embed, bias_dim=config["data"]["num_edge_types"]))
			elif kind == 'transformer':
				self.stack.append(great_transformer.Transformer(config["transformer"], shared_embedding=self.embed, bias_dim=None))
			elif kind == 'ggnn':
				self.stack.append(ggnn.GGNN(config["ggnn"], shared_embedding=self.embed, num_edge_types=config["data"]["num_edge_types"]))
			elif kind == 'rnn':
				self.stack.append(rnn.RNN(config["rnn"], shared_embedding=self.embed))
			else:
				raise ValueError("Unknown model provided:", kind)
	
	@tf.function(input_signature=[tf.TensorSpec(shape=(None, None, None), dtype=tf.int32), tf.TensorSpec(shape=(None, None), dtype=tf.int32), tf.TensorSpec(shape=(None, 4), dtype=tf.int32), tf.TensorSpec(shape=(), dtype=tf.bool)])
	def call(self, tokens, token_mask, edges, training):
		subtoken_embeddings = tf.nn.embedding_lookup(self.embed, tokens)
		subtoken_embeddings *= tf.expand_dims(tf.cast(tf.clip_by_value(tokens, 0, 1), dtype='float32'), -1)
		states = tf.reduce_mean(subtoken_embeddings, 2)
		for model in self.stack:
			if isinstance(model, rnn.RNN):
				states = model(states, training=training)
			elif isinstance(model, great_transformer.Transformer):
				mask = tf.cast(token_mask, dtype='float32')
				mask = tf.expand_dims(tf.expand_dims(mask, 1), 1)
				attention_bias = tf.stack([edges[:, 0], edges[:, 1], edges[:, 3], edges[:, 2]], axis=1)  # Reverse edge directions to match query-key direction.
				states = model(states, mask, attention_bias, training=training)
			else:
				states = model(states, edges, training=training)
		return tf.transpose(self.prediction(states), [0, 2, 1])  # Convert to [batch, 2, seq-length]
	
	@tf.function(input_signature=[tf.TensorSpec(shape=(None, 2, None), dtype=tf.float32), tf.TensorSpec(shape=(None, None), dtype=tf.int32), tf.TensorSpec(shape=(None,), dtype=tf.int32), tf.TensorSpec(shape=(None, None), dtype=tf.int32), tf.TensorSpec(shape=(None, None), dtype=tf.int32)])
	def get_loss(self, predictions, token_mask, error_locations, repair_targets, repair_candidates):
		seq_mask = tf.cast(token_mask, "float32")
		predictions += (1.0 - tf.expand_dims(seq_mask, 1)) * tf.float32.min
		is_buggy = tf.cast(tf.clip_by_value(error_locations, 0, 1), 'float32')  # 0 is the default position for non-buggy samples
		loc_predictions = predictions[:, 0]
		loc_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(error_locations, loc_predictions)
		loc_loss = tf.reduce_mean(loc_loss)
		loc_accs = tf.keras.metrics.sparse_categorical_accuracy(error_locations, loc_predictions)
		no_bug_pred_acc = tf.reduce_sum((1 - is_buggy) * loc_accs) / (1e-9 + tf.reduce_sum(1 - is_buggy))  # Take mean only on sequences without errors
		bug_loc_acc = tf.reduce_sum(is_buggy * loc_accs) / (1e-9 + tf.reduce_sum(is_buggy))  # Only on errors
		
		pointer_logits = predictions[:, 1]
		candidate_mask = tf.scatter_nd(repair_candidates, tf.ones(tf.shape(repair_candidates)[0]), tf.shape(pointer_logits))
		pointer_logits += (1.0 - candidate_mask) * tf.float32.min
		pointer_probs = tf.nn.softmax(pointer_logits)
		
		target_mask = tf.scatter_nd(repair_targets, tf.ones(tf.shape(repair_targets)[0]), tf.shape(pointer_probs))
		target_probs = tf.reduce_sum(target_mask * pointer_probs, -1)
		target_loss = tf.reduce_sum(is_buggy * -tf.math.log(target_probs + 1e-9)) / (1e-9 + tf.reduce_sum(is_buggy))  # Only on errors
		rep_accs = tf.cast(tf.greater_equal(target_probs, 0.5), 'float32')
		target_loc_acc = tf.reduce_sum(is_buggy * rep_accs) / (1e-9 + tf.reduce_sum(is_buggy))  # Only on errors
		joint_acc = tf.reduce_sum(is_buggy * loc_accs * rep_accs) / (1e-9 + tf.reduce_sum(is_buggy))  # Only on errors
		return (loc_loss, target_loss), (no_bug_pred_acc, bug_loc_acc, target_loc_acc, joint_acc)

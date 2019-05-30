import torch
import numpy as np
from torch import nn
from lib.models import voxel_encoder, pixor_encoder
from lib.models import middle, rpn
from lib.solver import build_losses
from lib.core.bbox import box_torch_ops
from lib.core.loss.loss import prepare_loss_weights, create_loss, get_pos_neg_loss, get_direction_target

from lib.engine import metrics
from lib.solver.losses import (WeightedSigmoidClassificationLoss,
                               WeightedSmoothL1LocalizationLoss,
                               WeightedSoftmaxClassificationLoss)

class VoxelNet(nn.Module):
    def __init__(self,
                 output_shape,
                 config,
                 num_classes=[2],
                 use_norm=True,
                 target_assigners=[],
                 voxel_generator=None,
                 name='voxelnet',
                 device=None,
                 logger=None):
        super().__init__()
        self._config = config
        self.name = name
        self.logger = logger
        self._num_classes = num_classes
        self._target_assigners = target_assigners
        self._voxel_generator = voxel_generator
        self._encode_background_as_zeros = config.model.loss.encode_background_as_zeros
        self._encode_rad_error_by_sin = config.model.loss.encode_rad_error_by_sin
        self._use_direction_classifier = config.model.decoder.auxiliary.use_direction_classifier
        ######## args ########
        self._pos_cls_weight = config.model.loss.pos_class_weight
        self._neg_cls_weight = config.model.loss.neg_class_weight
        self._loss_norm_type = config.model.loss.loss_norm_type
        self._direction_offset = config.model.decoder.auxiliary.direction_offset
        self._use_sigmoid_score = config.model.loss.use_sigmoid_score
        self._use_rotate_nms = config.model.post_process.use_rotate_nms
        self._multiclass_nms = config.model.post_process.use_multi_class_nms
        self._nms_score_threshold = config.model.post_process.nms_score_threshold

        self._nms_pre_max_size = config.model.post_process.nms_pre_max_size
        self._nms_post_max_size = config.model.post_process.nms_post_max_size
        self._nms_iou_threshold = config.model.post_process.nms_iou_threshold

        self._use_direction_classifier = config.model.decoder.auxiliary.use_direction_classifier
        logger.info("Using direction offset %f to fix aoe" %(self._direction_offset))
        self._box_coders = [
            target_assigner.box_coder for target_assigner in target_assigners
        ]

        self._dir_loss_function = WeightedSoftmaxClassificationLoss()
        self._ndim = config.box_coder.value.n_dim
        ######## classifization and localization function ########
        cls_loss_function, loc_loss_function = build_losses.build(config)

        self._loc_loss_function = loc_loss_function
        self._cls_loss_function = cls_loss_function
        self._cls_loss_weight = config.model.loss.classification_loss_weight
        self._loc_loss_weight = config.model.loss.localization_loss_weight
        self._direction_loss_weight = config.model.loss.direction_loss_weight
        self._post_center_range = config.model.post_process.post_center_limit_range

        ####### voxel feature encoder #######
        vfe_class_dict = {
            "VoxelFeatureExtractor": voxel_encoder.VoxelFeatureExtractor,
            "VoxelFeatureExtractorV2": voxel_encoder.VoxelFeatureExtractorV2,
            "VoxelFeatureExtractorV3": voxel_encoder.VoxelFeatureExtractorV3,
            "SimpleVoxel": voxel_encoder.SimpleVoxel,
            "PIXORFeatureLayer": pixor_encoder.PIXORFeatureLayer
        }
        vfe_class_name = config.model.encoder.vfe.type
        vfe_num_filters = config.model.encoder.vfe.num_filters
        with_distance = config.model.encoder.vfe.with_distance
        vfe_class = vfe_class_dict[vfe_class_name]
        self._vfe_class_name = vfe_class_name
        logger.info("Voxel Feature Encoder: {}".format(self._vfe_class_name))

        if "PIXOR" in self._vfe_class_name:
            self._voxel_feature_extractor = vfe_class(output_shape[:-1])
        else:
            self._voxel_feature_extractor = vfe_class(
            	config.model.encoder.vfe.num_input_features,
            	use_norm,
            	num_filters=vfe_num_filters,
            	with_distance=with_distance,
            	voxel_size=self._voxel_generator.voxel_size,
            	pc_range=self._voxel_generator.point_cloud_range)
        ######## middle ########
        middle_class_dict = {
            "SpMiddleFHD": middle.SpMiddleFHD,
            "None": None,
        }

        middle_class_name = config.model.encoder.middle.type
        middle_num_input_features = config.model.encoder.middle.num_input_features
        middle_num_filters_d1 = config.model.encoder.middle.num_filters_down1
        middle_num_filters_d2 = config.model.encoder.middle.num_filters_down2

        middle_class = middle_class_dict[middle_class_name]
        self._middle_class_name = middle_class_name
        logger.info("Middle class name: {}".format(middle_class_name))

        if "Pixor" in self._middle_class_name:
            self._middle_feature_extractor = middle_class(
                output_shape=output_shape,
                num_input_features=vfe_num_filters[-1])
        elif middle_class:
            self._middle_feature_extractor = middle_class(
                output_shape,
                use_norm,
                num_input_features=middle_num_input_features,
                num_filters_down1=middle_num_filters_d1,
                num_filters_down2=middle_num_filters_d2)
        else:
            logger.info("Pixor do not need Middle Layer")
        ######## rpn ########
        rpn_class_dict = {
            "RPNV2": rpn.RPNV2,
        }

        rpn_class_name = config.model.decoder.rpn.type
        rpn_num_input_features = config.model.decoder.rpn.num_input_features
        rpn_layer_nums = config.model.decoder.rpn.layer_nums
        rpn_layer_strides = config.model.decoder.rpn.downsample_layer_strides
        rpn_num_filters = config.model.decoder.rpn.downsample_num_filters
        rpn_upsample_strides = config.model.decoder.rpn.upsample_layer_strides
        rpn_num_upsample_filters = config.model.decoder.rpn.upsample_num_filters

        num_groups = config.model.decoder.rpn.num_groups
        use_groupnorm = config.model.decoder.rpn.group_norm

        rpn_class = rpn_class_dict[rpn_class_name]
        self._rpn_class_name = rpn_class_name
        logger.info("RPN class name: {}".format(self._rpn_class_name))

        self._rpn = rpn_class(
            use_norm=True,
            num_classes=num_classes,
            layer_nums=rpn_layer_nums,
            layer_strides=rpn_layer_strides,
            num_filters=rpn_num_filters,
            upsample_strides=rpn_upsample_strides,
            num_upsample_filters=rpn_num_upsample_filters,
            num_input_features=rpn_num_input_features,
            num_anchor_per_locs = [
                target_assigner.num_anchors_per_location
                for target_assigner in target_assigners
            ],
            encode_background_as_zeros=self._encode_background_as_zeros,
            use_direction_classifier=self._use_direction_classifier,
            use_groupnorm=use_groupnorm,
            num_groups=num_groups,
            box_code_sizes=[
                target_assigner.box_coder.code_size
                for target_assigner in target_assigners
            ],
            logger=logger,
            )
        ######## metrics ########
        self.rpn_acc = metrics.Accuracy(
            dim=-1,
            encode_background_as_zeros=self._encode_background_as_zeros)
        self.rpn_precision = metrics.Precision(dim=-1)
        self.rpn_recall = metrics.Recall(dim=-1)
        self.rpn_metrics = metrics.PrecisionRecall(
            dim=-1,
            thresholds=config.model.loss.rpn_thresholds,
            use_sigmoid_score=self._use_sigmoid_score,
            encode_background_as_zeros=self._encode_background_as_zeros)

        self.rpn_cls_loss = metrics.Scalar()
        self.rpn_loc_loss = metrics.Scalar()
        self.rpn_total_loss = metrics.Scalar()


        self.register_buffer("global_step", torch.LongTensor(1).zero_())
    def update_global_step(self):
        self.global_step += 1

    def clear_global_step(self):
        self.global_step.zero_()

    def get_global_step(self):
        return int(self.global_step.cpu().numpy()[0])

    def forward(self, example):
        voxels = example["voxels"]
        num_points = example["num_points"]
        coordinates = example["coordinates"]
        batch_anchors = example["anchors"]
        batch_size_dev = batch_anchors[0].shape[0]

        if "PIXOR" in self._vfe_class_name:
            voxel_features = self._voxel_feature_extractor(
                voxels, num_points, coordinates, batch_size_dev)
        else:
            voxel_features = self._voxel_feature_extractor(
                voxels, num_points, coordinates)
        if "PIXOR" not in self._vfe_class_name:
            spatial_features = self._middle_feature_extractor(
                voxel_features, coordinates, batch_size_dev)
        else:
            spatial_features = voxel_features
        # (spatial_features.sum(dim=1)[0].detach().cpu().numpy() / 41.0).tofile(open("./bev.bin", "wb"))
        # example['labels'][1][0].detach().cpu().numpy().tofile(open("labels.bin", "wb"))
        # example['gt_boxes'][1][0].tofile(open("gt_boxes.bin", "wb"))
        predict_dicts = self._rpn(spatial_features)
        rets = []
        for task_id, pred_dict in enumerate(predict_dicts):
            if self.training:
                box_preds = pred_dict["box_preds"]
                cls_preds = pred_dict["cls_preds"]

                labels = example["labels"][task_id]
                reg_targets = example["reg_targets"][task_id]

                cls_weights, reg_weights, cared = prepare_loss_weights(labels,
                                                                       pos_cls_weight=self._pos_cls_weight,
                                                                       neg_cls_weight=self._neg_cls_weight,
                                                                       loss_norm_type=self._loss_norm_type,
                                                                       dtype=voxels.dtype)

                cls_targets = labels * cared.type_as(labels)
                cls_targets = cls_targets.unsqueeze(-1)

                loc_loss, cls_loss = create_loss(self._loc_loss_function,
                                                 self._cls_loss_function,
                                                 box_preds=box_preds,
                                                 reg_targets=reg_targets,
                                                 reg_weights=reg_weights,
                                                 cls_preds=cls_preds,
                                                 cls_targets=cls_targets,
                                                 cls_weights=cls_weights,
                                                 num_class=self._num_classes[task_id],
                                                 encode_rad_error_by_sin=self._encode_rad_error_by_sin,
                                                 encode_background_as_zeros=self._encode_background_as_zeros,
                                                 box_code_size=self._box_coders[task_id].code_size)


                loc_loss_reduced = loc_loss.sum() / batch_size_dev
                loc_loss_reduced *= self._loc_loss_weight

                cls_pos_loss, cls_neg_loss = get_pos_neg_loss(cls_loss, labels)
                cls_loss_reduced = cls_loss.sum() / batch_size_dev
                cls_loss_reduced *= self._cls_loss_weight

                loss = loc_loss_reduced + cls_loss_reduced

                if self._use_direction_classifier:
                    dir_targets = get_direction_target(example["anchors"][task_id],
                                                       reg_targets,
                                                       dir_offset=self._direction_offset)
                    dir_logits = pred_dict["dir_cls_preds"].view(batch_size_dev, -1, 2)
                    weights = (labels > 0).type_as(dir_logits)
                    weights /= torch.clamp(weights.sum(-1, keepdim=True), min=1.0)
                    dir_loss = self._dir_loss_function(dir_logits,
                                                       dir_targets,
                                                       weights=weights)
                    dir_loss = self._direction_loss_weight * dir_loss.sum() / batch_size_dev
                    loss += dir_loss
                ret = {
                    "loss": loss,
                    "cls_loss": cls_loss,
                    "loc_loss": loc_loss,
                    "cls_pos_loss": cls_pos_loss,
                    "cls_neg_loss": cls_neg_loss,
                    "cls_preds": cls_preds,
                    "dir_loss_reduced": dir_loss if self._use_direction_classifier else None,
                    "cls_loss_reduced": cls_loss_reduced,
                    "loc_loss_reduced": loc_loss_reduced,
                    "cared": cared,
                      }
                rets.append(ret)
            else:
                with torch.no_grad():
                    ret = self.predict(example, pred_dict, task_id)
                    rets.append(ret)
        if self.training:
            return rets
        else:
            num_tasks = len(rets)
            ret_list = []
            num_preds = len(rets)
            num_samples = len(rets[0])
            for i in range(num_samples):
                ret = {}
                for k in rets[0][i].keys():
                    if k in ['box3d_lidar', 'scores']:
                        ret[k] = torch.cat([ret[i][k] for ret in rets])
                    elif k in ['label_preds']:
                        flag = 0
                        for j, num_class in enumerate(self._num_classes):
                            rets[j][i][k] += flag
                            flag += num_class
                        ret[k] = torch.cat([ret[i][k] for ret in rets])
                    elif k == 'metadata':
                        ret[k] = rets[0][i][k]
                ret_list.append(ret)
            return ret_list



    def metrics_to_float(self):
        self.rpn_acc.float()
        self.rpn_metrics.float()
        self.rpn_cls_loss.float()
        self.rpn_loc_loss.float()
        self.rpn_total_loss.float()

    def update_metrics(self, cls_loss, loc_loss, cls_preds, labels, sampled,
                       task_id):
        batch_size = cls_preds.shape[0]
        num_class = self._num_classes[task_id]
        if not self._encode_background_as_zeros:
            num_class += 1
        cls_preds = cls_preds.view(batch_size, -1, num_class)
        rpn_acc = self.rpn_acc(labels, cls_preds, sampled).numpy()[0]
        prec, recall = self.rpn_metrics(labels, cls_preds, sampled)
        prec = prec.numpy()
        recall = recall.numpy()
        rpn_cls_loss = self.rpn_cls_loss(cls_loss).numpy()[0]
        rpn_loc_loss = self.rpn_loc_loss(loc_loss).numpy()[0]
        ret = {
            "loss": {
                "cls_loss": float(rpn_cls_loss),
                "cls_loss_rt": float(cls_loss.data.cpu().numpy()),
                'loc_loss': float(rpn_loc_loss),
                "loc_loss_rt": float(loc_loss.data.cpu().numpy()),
            },
            "rpn_acc": float(rpn_acc),
            "pr": {},
        }
        for i, thresh in enumerate(self.rpn_metrics.thresholds):
            ret["pr"][f"prec@{int(thresh*100)}"] = float(prec[i])
            ret["pr"][f"rec@{int(thresh*100)}"] = float(recall[i])
        return ret
    def clear_metrics(self):
        self.rpn_acc.clear()
        self.rpn_metrics.clear()
        self.rpn_cls_loss.clear()
        self.rpn_loc_loss.clear()
        self.rpn_total_loss.clear()


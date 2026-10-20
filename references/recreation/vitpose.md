# ViTPose 全身观测与迁移

独立复刻默认使用ViTPose++拆出的WholeBody133任务专家与任务头。身体17、足6、脸68、左手21、右手21全部保留；MotionBERT只消费派生H36M17不代表可以删除其余观测。二维confidence不是可见性、身份或接触真值。

## 官方Demo与运行契约

上游[ViTPose](https://github.com/ViTAE-Transformer/ViTPose/tree/c050ed29112da7704797cc1a65af0234b525010d)的WholeBody配置、推理和翻转测试是适配依据。转换权重可定位到[easy_ViTPose固定模型版本](https://huggingface.co/JunkyByte/easy_ViTPose/tree/e83805274e89428969355ec4afffcbc413e79188)，其运行时默认UDP与原官方MSRA配置不同，不能直接照搬解码。

- B模型输入宽192×高256，输出133×64×48热图。普通COCO17模型不能修改schema后充当全身模型。
- 输入BGR图像；inclusive xyxy框转xywh使用`+1`，长宽比适配、padding1.25；官方TopDownAffine、转RGB、ImageNet归一化。
- MSRA/default坐标解码；原图与水平翻转热图恢复左右配对后平均；翻转热图右移一列。此适配器不声称支持尚未验证的UDP配置。
- `vendor_mmpose_msra.py`保留官方所选函数原计算，来源/版本和提取符号见`vitpose-provenance.json`，许可证随包提供。
- H36M头10按官方COCO映射使用双眼中点代理，并明确标记非实测头顶；禁止继承Halpe17作为新COCO足点。

## 本地调用

先准备明确本地模型和hash manifest，初始化不下载权重：

```json
{"family":"vitpose","schema":"coco_wholebody133","onnx":"<外部模型绝对路径>","sha256":"<64位哈希>","input_size_wh":[192,256],"decoder":"mmpose-msra-default"}
```

当前B型ONNX为360242862字节，SHA256为`017c08e161bfa2c72a96297b3b6de975ecd83c65c4fd56fb5b0bfc1298709069`。对应原生pth为360153207字节，SHA256为`3365f2f33cd11267e1f447fe22a1669f54dfbe8507651c88727d94287ab2522a`。权重来源为第三方转换，必须标明，不能误称官方直接发布ONNX。

轨迹文件绑定`source_sha256`、实际`fps`、`crop_xywh`，每行含连续从零起的`source_frame_index`和`roles`。每个角色含稳定`id`、裁切内inclusive `bbox_xyxy`、`identity_provenance`和`uncertainty`。检测器框可复用，候选下标不能当稳定ID；缺失bbox明确产生missing观察，不自动用全画面替代。

```text
<pose阶段python> scripts/recreation/vitpose_inference.py --source <原片> --tracks <身份框轨迹.json> --model-manifest <模型.json> --output <新观测.json>
```

脚本本地推理，全帧覆盖校验、真实解码时间戳、133点原始分数、手脚脸分组和派生H36M17一并输出。现有输出拒绝覆盖。没有模型静默下载、网络上传、付费或旧管线导入。

## 固定形状与接触边界

`retarget.fixed_shape_fk`沿原始3D骨段方向和每角色固定骨长重建，避免按二维投影重新开方选深度符号引起瞬间翻腿。固定骨长只是局部形状；还必须检查世界缩放、相机、根轨迹、实际足底与共享地面。

`retarget.two_bone_ik`采用标准两骨骼Target＋Hint约束，与[Unity官方双骨IK的输入契约](https://docs.unity3d.com/Packages/com.unity.animation.rigging@1.2/manual/constraints/TwoBoneIKConstraint.html)一致；本实现独立使用余弦定理解算，没有复制Unity代码。它保持两段长度，明确返回不可达残差，Hint保留膝弯曲方向。它不负责产生接触标签或标定地面；不可把错标签的精确锁脚当复刻通过。

不能把WHAM一段速度补偿函数声称成完整WHAM：它需要可靠足接触概率、根旋转和足点。原官方完整路径的GPU、DPVO、SMPL许可与资产必须真实满足；无资产时报告边界。不可逐帧把最低脚压地，跳跃/摆腿不能锁成支撑。自动接触候选保留不确定性，滑步、穿地、动作投影和速度分别检查。

## 更新回归

每次模型/解码更换需验证原生与导出热图、解码坐标一致，并覆盖真实困难帧；数字一致不等于观感通过。全片迁移不能只跑抽样再拼回旧模型中段。保留身份纠正、遮挡ledger、原音、镜头、完整时长、手脚脸、情绪、独立初始化和干净分发。数据/工程回归、复刻保真和用户接受分别记录。

渲染用头部转向保留原有连续性能力：`retarget.bounded_angle_guide`处理角度环绕与显式调用方限速，参数随帧率和项目契约确定；不能把渲染估计写回原始姿态。保留头颈连接和足爪绑定，并检查实际网格。取景调整不能追逐肢体摆动造成虚假缩放；需要完整身体时优先评估全片固定的共同取景变换，同时保留真实镜头运动，明确构图变化。

## 时序与分歧准入

官方[Tracking Demo](https://github.com/ViTAE-Transformer/ViTPose/blob/c050ed29112da7704797cc1a65af0234b525010d/demo/top_down_pose_tracking_demo_with_mmdet.py)支持相邻帧关联及可选 One Euro；不是世界运动、惯性补全或接触求解器。其 tracking API 调用的默认 OKS sigmas 为 COCO17，不能直接对133点使用；需使用明确的身体17子集或适配完整schema参数。已人工校正的身份轨迹不能因切换模型被默认贪心关联覆盖。

`temporal.py`是纯标准库派生控制层，不增加初始化依赖，也不覆盖原始133点：

- `resolved_axis(vector, score, grid_px=...)`要求方向杆长至少两个源坐标热图格。`grid_px`根据该帧实际仿射框和热图大小计算。此为保守的工程分辨率门槛，不是准确率保证；高分不能弥补亚格分辨率或遮挡。
- `AngleGuide(max_speed=..., initial_angle=...)`使用[One Euro 原论文公式](https://gery.casiez.net/1euro/)及真实秒级时间，角度先展开；缺失传`None`并明确返回unknown/held_unknown，重获时不瞬间跳向新角度。若渲染器在第一笔观测之前已有默认朝向，必须显式传入这个初值并标成先验，否则首次观测仍可能造成开头突跳。显式角速度上限是项目的渲染保护策略，不是模型恢复的运动学真值。按静止抖动与快速动作滞后分别验证参数，不允许只看更平滑。
- `direction_consensus(directions, weights, threshold_degrees=..., min_weight=...)`检查同身份、同源帧、同骨段的窗口方向。显著分歧时返回`direction=None`和conflict；不得将XYZ平均后的中间姿态当作解决了深度歧义。弱窗口边缘的排除阈值同样是显式项目策略。
- `face_controls(face68, scores68, grid_px=...)`只在眼、口有足够像素支持时给出开合比例；不分类情绪，不自动生成笑容、眨眼。返回unresolved时不得宣称人类情绪已驱动动物。真实面部深度、侧背脸和情绪仍需单独证据。
- `palm_axis(hand21, scores21, grid_px=...)`保留腕到掌指关节中心的二维方向及深度未知标记；不能直接将屏幕角度作为三维掌旋转。投影缩短或深度未知时保留前臂绑定，不强行转爪。

固定骨长还不等于固定体型：胸腹宽高与骨架姿态分开，固定形状用正交身体坐标架变换；肩距弦长不能逐帧缩放胸腹。姿态仍控制重心、方向、关节和连接。实际网格尺寸及连接需单独回归。

接触修复同时比较源动作投影、修正量、支撑状态和足底残差。候选地面残差归零但原动作误差变大，应否决该修复。形状旋转改变足底时需重新检查网格；不得复用旧接触通过报告。缺乏可靠深度/支撑证据时保留HOLD，不能靠重新锁脚掩盖滑动。

## 受限窗口拟合与物理补全

`kinematic_fit.py`属于独立`fit`阶段，调用拟合时才加载NumPy/SciPy；导入模块及`audit_support`不需要第三方包，不改变inspect/pose阶段依赖。它沿固定骨长做FK，联合可见投影、已有三维姿态先验、接触高度、非穿地和时序约束。不运行HuMoR、SMPL或任何新模型；[HuMoR官方拟合损失](https://github.com/davrempe/humor/blob/main/humor/fitting/fitting_loss.py)与[优化器](https://github.com/davrempe/humor/blob/main/humor/fitting/motion_optimizer.py)仅作成熟约束组织方式的依据。

1. `fit_window`要求明确的世界相机契约、拓扑、同一角色固定骨形、观测权重、实际足底相对踝偏移及冻结帧。原始观测不覆盖。`fit_root=False`用于上身局部校正，防止修手臂时带动根位置和腿。
2. 冻结范围以**允许修改的源帧**确定，不只冻结窗口首尾三帧。否则有标注的38帧变好，也可能让前后原本贴地的帧新增浮空。回归检查覆盖全片和未修改角色，不能只看拟合窗口或训练标签。
3. 默认`temporal_mode='correction'`平滑修正量，以保留已有运动；`'motion'`按真实三维运动的二阶差分作恒速惯性先验，适用于信息不足时的物理补全。后者不能自动宣称动作峰值保留，须另外检查可见动作的幅度、相位与衔接。
4. **观测复刻**与**物理补全**分开保存。前者用可核对的源接触证据；后者可按用户允许的预测意图，为画外/遮挡足部制定支撑假设，但必须标记inferred、保存选择依据。约束刚度是动画求解策略，不等于源接触置信度。
5. 补全的可见投影改动预算应事先记录；可参考当前热图空间支持量级制定工程预算，但不能把热图格当作校准后的统计误差界。不能为把报告改绿而不断放宽门槛。候选按可见动作、实际网格、全片作用域、接触和用户观感分别验收。
6. 求解器的成功标志不是效果验收。迭代上限停止的有限可行点可单独渲染排错预览，但必须保留未收敛状态，不能冒称收敛、自动批准或用它替换已接受候选。

`audit_support`输入**全片实际网格**的双足间隙及独立的grounded/flight/unknown源状态，检查无接触候选时的双脚悬空。unknown不得自动算作腾空通过；几何通过与源状态覆盖分别返回。最低网格顶点会因脚转向切换，切向速度还应对固定材料点/接触面核查，不能直接把它当真实滑步量。

### CPU膝弯与固定接触点控制

`motion_control.py`仅使用Python标准库，不引入Torch、CUDA或旧业务管线。`BendGuide`将膝弯方向沿髋到踝的轴平行运输；接近伸直或低置信度时不采用不稳定的新方向，重获按调用方显式角速度上限变化。首次伸直且无历史时返回unknown；轴恰好反向单独标记歧义。输出只是重定向控制，仍须通过可见动作投影、幅度、相位与完整事件衔接准入，不能全片强行平滑。

`ContactGuide`要求明确stance/swing/pivot/moving_contact/flight/unknown以及证据来源。按真实秒级时间做进入确认与退出滞回；unknown不等于腾空，转脚和移动接触不做静态锁定。inferred默认不锁，确需物理补全时显式启用并保存推断依据。`point_velocity`测量同一固定材料点的法向和切向速度；源画面二维脚点速度只能作为接触候选线索，不能冒充材料点世界速度。

`fit_window`的可选`contact_targets_world`与`contact_position_weights`必须成对提供；没有显式权重不启用位置锁。`contact_position_offsets`用于同一接触事件中固定网格顶点/材料点相对踝的偏移，与逐帧最低点高度偏移`sole_offsets`分开。接触点身份在事件内不得切换；转脚、离地或未知时释放。拟合改变脚朝向后需用新实际网格复测材料点和非穿地，旧偏移得到的低残差不构成验收。

`motion_control.gate_contact_target_plan`用于**渐入/渐出之后、实际IK之前**复核最终位置锁请求。每次调用只覆盖同一角色和同一固定材料点，逐帧绑定mode、证据来源、源帧与原请求权重；移动、转脚、腾空、未知均不得因未来静止区间的反向渐入而获得静态锁。inferred默认不锁，允许推断锁须显式选择。保存被阻止的帧和原请求，不把门控当成地面/接触真值或完整事件自然度验收。旧候选先精确重放、逐层追溯，再从错误修正前的先验重建；不得只在已被错误IK改变的轨迹上叠加平滑。

身体重拟合后，头部还需检查实际渲染，不能看到JSON里有某种绑定字段就默认走到了该路径。`retarget.preserve_pinhole_projection`在明确的固定针孔相机下，沿原头部中心视线更新深度，避免身体深度适配把头在画面中带走；头部尺度、朝向和原始脸观测另行保持。它只保屏幕中心，不保证脸部情绪或完整三维头颈真值。共享图像变换之后仍需用实际网格投影逐帧复核。

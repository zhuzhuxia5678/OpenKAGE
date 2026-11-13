"""openkage.orchestrate: 泛型编排层（任务域无关）。

- loop.py       —— run_arm_batch（一 arm 一 batch 采样+反思）/ eval_readonly（evolving vs frozen）
                   泛型版，对 TaskDomain 多态调用。
- consolidate.py —— N 份副脑图谱融合进主脑图谱（本就域无关）。
"""

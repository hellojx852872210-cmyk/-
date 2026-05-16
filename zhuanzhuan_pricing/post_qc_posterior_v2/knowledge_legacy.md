# legacy knowledge

- 历史问题：把 postQcDiffItems 当作“全是拦截项”，导致误抓。
- 关键语义：同一 qcItem 可能返回整段键值串，需要拆分子项再比对 ori/post。
- 常见重复来源：多账号重复命中、同子项重复上报。
- 本目录仅保留知识，不参与运行逻辑。

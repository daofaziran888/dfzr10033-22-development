"""种子数据：每个模块一条记录，精简格式。"""
from typing import Any

# 通用种子字段模板
def _seed(id: int, fields: dict[str, str]) -> dict[str, Any]:
    return {"id": id, "status": "草稿", "pending": True, "abnormal": False, **fields}


def _active(id: int, fields: dict[str, Any]) -> dict[str, Any]:
    """业务上已经生效、不再待处理的辅助行。"""
    return {"id": id, "status": "可用", "pending": False, "abnormal": False, **fields}


SEED_ROWS: dict[str, list[dict[str, Any]]] = {
    "source_registry": [
        _seed(1, {"源编号":"SOUR-0001","源类型":"mysql","连接地址":"db://demo","驱动配置":"mysql_driver","数据库名":"demo_db","认证方式":"token","接入状态":"待验证","最近同步":"2026-11-01"}),
        # 第二份来源仍在生产运行，和 SOUR-0001 拥有同名字段 user_id。
        {"id":2,"status":"已注册","pending":False,"abnormal":False,
         "源编号":"SOUR-0002","源类型":"postgresql","连接地址":"db://demo-secondary",
         "驱动配置":"postgresql_driver","数据库名":"demo_db_2","认证方式":"token",
         "接入状态":"已接入","最近同步":"2026-11-05"},
    ],
    "semantic_model": [
        {"id":1,"status":"草稿","pending":True,"abnormal":False,
         "模型编号":"SEMA-0001","模型名称":"订单语义模型","源表映射":"source_registry:1",
         "字段别名":"user_id,order_amount,region","度量定义":"COUNT(*),SUM(order_amount)",
         "维度定义":"region,category","计算口径":"标准口径","版本序号":"v1.2"},
        # 同一字段别名 user_id 的第二份来源模型，处于已投产状态。
        {"id":2,"status":"已投产","pending":False,"abnormal":False,
         "模型编号":"SEMA-0002","模型名称":"事件语义_备用源","源表映射":"source_registry:2",
         "字段别名":"user_id,order_amount,region","度量定义":"COUNT(*),SUM(order_amount)",
         "维度定义":"region,category","计算口径":"标准口径","版本序号":"v1.0"},
    ],
    "query_federation": [_seed(1, {"计划编号":"QUER-0001","参与来源":"SOUR-0001","关联键":"id","下推策略":"FULL_PUSHDOWN","数据量估算":"1000","并行度":"4","超时上限":"30s","结果合并":"DIRECT"})],
    "olap_cube": [_seed(1, {"立方编号":"OLAP-0001","立方名称":"多维立方样例","维度列表":"region,category","度量列表":"revenue,orders","聚合类型":"SUM","分区策略":"dt","刷新周期":"3600","数据行数":"0"})],
    "metric_factory": [_seed(1, {"指标编号":"METR-0001","指标名称":"指标工厂样例","数据来源":"olap_cube:1","计算公式":"SUM(revenue)","聚合窗口":"daily","精度模式":"exact","依赖立方":"1","最后产出":"2026-11-01"})],
    "dimension_tree": [_seed(1, {"层级编号":"DIME-0001","维度名称":"维度树样例","层级路径":"region>city","叶子字段":"city","汇总规则":"SUM","缓慢变化策略":"SCD2","行数预估":"1000","版本标签":"v1"})],
    "aggregate_cache": [_seed(1, {"配置编号":"AGGR-0001","目标立方":"olap_cube:1","缓存粒度":"2维x2度","TTL秒数":"3600","预热策略":"LAZY","命中阈值":"0.8","淘汰算法":"LRU","缓存大小":"1024"})],
    "query_rewrite": [
        {"id":1,"status":"已生效","pending":False,"abnormal":False,
         "规则编号":"QUER-0001","原始模式":"SELECT * FROM","替换模式":"SELECT /*optimized*/ FROM",
         "适用模型":"semantic_model:1","改写条件":"always","代价阈值":"1000","命中次数":"0","最后命中":"-"},
        # 旧规则只写了裸列名 user_id，没有绑定模型/来源；由两边各自的字段样本限定适用范围。
        {"id":2,"status":"已生效","pending":False,"abnormal":False,
         "规则编号":"QUER-0002","原始模式":"user_id","替换模式":"CAST(user_id AS VARCHAR)",
         "适用模型":"","改写条件":"always","代价阈值":"1000","命中次数":"0","最后命中":"-"},
        # 另一份来源上的生产规则，和本次更名的 user_id 无关，投产期间必须照常运行。
        {"id":3,"status":"已生效","pending":False,"abnormal":False,
         "规则编号":"QUER-0003","原始模式":"order_amount","替换模式":"order_amount::numeric",
         "适用模型":"semantic_model:2","改写条件":"always","代价阈值":"1000","命中次数":"0","最后命中":"-"},
    ],
    "time_window": [_seed(1, {"窗口编号":"TIME-0001","窗口类型":"滑动","滑动宽度":"3600","滑动步长":"600","对齐基准":"epoch","缺值策略":"fill_prev","窗口别名":"1h_window","最后计算":"-"})],
    "dataset_slice": [_seed(1, {"切片编号":"DATA-0001","源立方":"1","维度过筛":"region=CN","度量过滤":"revenue>0","切片标签":"active","行数统计":"500","创建人":"admin","最后刷新":"-"})],
    "drill_config": [_seed(1, {"路径编号":"DRIL-0001","起始维度":"region","目标维度":"city","下钻类型":"hierarchy","关联键":"region_id","跳转规则":"exact","展示模板":"table","最后使用":"-"})],
    "result_mart": [_seed(1, {"集市编号":"RESU-0001","集市名称":"结果集市样例","数据来源":"result_cache","主题域":"analytics","行数统计":"0","刷新周期":"manual","下游消费":"dashboard","最后刷新":"-"})],
    "benchmark_set": [_seed(1, {"基线编号":"BENC-0001","基线名称":"基准集样例","数据范围":"2026-09","基线时段":"2026-09-01","对比维度":"region","指标数值":"1000","方差阈值":"0.1","锁定状态":"unlocked"})],
    "query_audit": [_seed(1, {"记录编号":"QUER-0001","查询语句":"SELECT * FROM demo","执行耗时":"100ms","扫描行数":"1000","返回行数":"10","数据来源":"query_federation","操作用户":"admin","查询时间":"2026-11-01"})],
    "cost_govern": [_seed(1, {"策略编号":"COST-0001","适用角色":"analyst","查询上限":"10000","并发上限":"10","月度配额":"100000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-11"})],
    "freshness_mon": [_seed(1, {"规则编号":"FRES-0001","目标数据集":"all","SLA秒数":"3600","检测频率":"hourly","延迟阈值":"600","告警级别":"WARNING","触发次数":"0","最后检查":"-"})],
    "data_exporter": [_seed(1, {"任务编号":"DATA-0001","数据来源":"result_cache","筛选条件":"all","输出格式":"csv","字段列表":"*","排序规则":"id","导出上限":"1000","接收地址":"local://"})],
    "schedule_job": [_seed(1, {"任务编号":"SCHE-0001","目标立方":"1","Cron表达式":"0 */1 * * *","时区设置":"Asia/Shanghai","依赖检查":"none","超时上限":"600","失败通知":"email","下次执行":"-"})],
    "data_lake_read": [_seed(1, {"路径编号":"DATA-0001","存储位置":"s3://data-lake/prod","文件格式":"parquet","分区模式":"dt","Schema推导":"","文件列表":"","最后扫描":"","数据量":""})],
    "result_cache": [_seed(1, {"条目编号":"RESU-0001","查询摘要":"sample query","结果哈希":"abc123","缓存命中":"5","过期时间":"2027-01-01","存储大小":"100","创建时间":"2026-11-01","最后命中":"2026-11-01"})],
    "data_contract": [_seed(1, {"契约编号":"DATA-0001","数据集":"analytics","Schema约束":"id,name,value","SLA定义":"每小时更新","更新频率":"3600","负责人":"admin","消费者":"dashboard","生效时间":"2026-11-01"})],
    "ab_config": [_seed(1, {"实验编号":"AB_C-0001","实验名称":"实验配置样例","分流比例":"50:50","指标集合":"revenue","对照组":"baseline","实验组":"variant","置信水平":"0.95","运行期间":"2026-11"})],

    # ------------------------------------------------------------------
    # 辅助查询平面（不是工作台业务模块）：字段身份映射、规则样本与变更清单。
    # ------------------------------------------------------------------
    "model_field_mapping": [
        # 模型 1 v1.0 的历史映射：曾用列 uid 承载别名 user_id，2026-06 改名为 user_id。
        _active(1, {"model_id":1,"model_no":"SEMA-0001","model_version":"v1.0",
                   "source_id":1,"source_no":"SOUR-0001","字段别名":"user_id",
                   "列名":"uid","is_current":False,"valid_from":"2026-01-01","valid_to":"2026-05-31"}),
        # 模型 1 当前映射：本次要投产 user_id -> buyer_id 的更名。
        _active(2, {"model_id":1,"model_no":"SEMA-0001","model_version":"v1.2",
                   "source_id":1,"source_no":"SOUR-0001","字段别名":"user_id",
                   "列名":"user_id","is_current":True,"valid_from":"2026-06-01","valid_to":""}),
        _active(3, {"model_id":1,"model_no":"SEMA-0001","model_version":"v1.2",
                   "source_id":1,"source_no":"SOUR-0001","字段别名":"order_amount",
                   "列名":"order_amount","is_current":True,"valid_from":"2026-06-01","valid_to":""}),
        _active(4, {"model_id":1,"model_no":"SEMA-0001","model_version":"v1.2",
                   "source_id":1,"source_no":"SOUR-0001","字段别名":"region",
                   "列名":"region","is_current":True,"valid_from":"2026-06-01","valid_to":""}),
        # 另一份仍在运行的来源：同一别名 user_id 对应另一列数据。
        _active(5, {"model_id":2,"model_no":"SEMA-0002","model_version":"v1.0",
                   "source_id":2,"source_no":"SOUR-0002","字段别名":"user_id",
                   "列名":"user_id","is_current":True,"valid_from":"2026-02-01","valid_to":""}),
        _active(6, {"model_id":2,"model_no":"SEMA-0002","model_version":"v1.0",
                   "source_id":2,"source_no":"SOUR-0002","字段别名":"order_amount",
                   "列名":"order_amount","is_current":True,"valid_from":"2026-02-01","valid_to":""}),
        _active(7, {"model_id":2,"model_no":"SEMA-0002","model_version":"v1.0",
                   "source_id":2,"source_no":"SOUR-0002","字段别名":"region",
                   "列名":"region","is_current":True,"valid_from":"2026-02-01","valid_to":""}),
    ],
    # 同一条旧规则在两边来源各自的样本：source 1 的取值会被改写，source 2 保持原值。
    "rewrite_rule_sample": [
        _active(1, {"rule_id":2,"source_id":1,"source_no":"SOUR-0001","字段别名":"user_id",
                   "列名":"user_id","样本值":["1001","1002"],"改写后":["'1001'","'1002'"],
                   "样本说明":"数值型样本，旧规则会改写该侧取值"}),
        _active(2, {"rule_id":2,"source_id":2,"source_no":"SOUR-0002","字段别名":"user_id",
                   "列名":"user_id","样本值":["U-77","U-88"],"改写后":["U-77","U-88"],
                   "样本说明":"文本型样本，旧规则对该侧取值保持不变"}),
        _active(3, {"rule_id":3,"source_id":2,"source_no":"SOUR-0002","字段别名":"order_amount",
                   "列名":"order_amount","样本值":["12.5","30.0"],"改写后":["12.50","30.00"],
                   "样本说明":"另一来源的生产规则样本"}),
        _active(4, {"rule_id":1,"source_id":1,"source_no":"SOUR-0001","字段别名":"",
                   "列名":"","样本值":["SELECT * FROM demo"],"改写后":["SELECT /*optimized*/ FROM demo"],
                   "样本说明":"整句改写，不涉及字段身份"}),
    ],
    "field_change_list": [
        {"id":1,"status":"待验证","pending":True,"abnormal":False,
         "model_id":1,"model_no":"SEMA-0001","model_version":"v1.2","source_id":1,
         "source_no":"SOUR-0001","字段别名":"user_id","目标别名":"buyer_id",
         "变更类型":"更名","校验结果":"","涉及规则":"","可用时间":""},
        # 同一别名在另一来源上的列，尚未定位到可确认的字段身份。
        {"id":2,"status":"待定位","pending":True,"abnormal":False,
         "model_id":2,"model_no":"SEMA-0002","model_version":"v1.0","source_id":2,
         "source_no":"SOUR-0002","字段别名":"user_id","目标别名":"user_id",
         "变更类型":"关联核对","校验结果":"","涉及规则":"","可用时间":""},
    ],
    # 规则样本验证的历史留档，便于回归时对比上一轮结论。
    "rewrite_rule_validation": [],
}
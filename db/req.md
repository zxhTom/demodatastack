# 压测接口
## 曲线数据

接口：hes-web-api/api/profile/profileLog/page?BACKTIME=METER_TMNL_PROFILE_PAGE

参数：
```json
{
    "pageNum": 1,
    "pageSize": 100,
    "sortFields": [],
    "orgNo": "100",
    "grpId": null,
    "grpType": null,
    "lineId": null,
    "tmnlId": null,
    "meterId": null,
    "serialNo": null,
    "groupIds": [
        40930000011
    ],
    "rawType": "06",
    "rangePicker": null,
    "ranges": [
        "2025-06-01T08:35:25.754Z",
        "2025-06-07T08:35:25.754Z"
    ],
    "startTime": "2025-06-01 00:00:00",
    "endTime": "2025-06-07 23:59:59",
    "meterIds": [
        14232,
        15833
    ],
    "defaultGroup": "__93%",
    "ntmnlTypeCode": "01"
}
```

- 这个接口中的参数groupIds不用改变。你在压测的时候要将ranges，startTime,endTime ，meteerIds换掉作为每轮测试参数
- 比如你可以在meterIds不变情况优先改变ranges，startTime，endTime，这作为时间批次
- 时间批次轮完在将meterIds论。meterIds就是不断的添加id ， 这个要添加c_meter 中真实有效的ID

## 事件数据

接口： /hes-web-api/api/event/eventLog/page?BACKTIME=METER_TMNL_EVENT_PAGE

参数

```json
{
    "pageNum": 1,
    "pageSize": 100,
    "sortFields": [],
    "groupIds": [],
    "rawType": "05",
    "ranges": [
        "2026-06-07T08:47:23.093Z",
        "2026-07-07T08:47:23.093Z"
    ],
    "startTime": "2026-06-07 00:00:00",
    "endTime": "2026-07-07 23:59:59",
    "meterIds": [
        14232,
        15831,
        15833
    ]
}
```


- 这个接口中的参数groupIds不用改变。你在压测的时候要将ranges，startTime,endTime ，meteerIds换掉作为每轮测试参数
- 比如你可以在meterIds不变情况优先改变ranges，startTime，endTime，这作为时间批次
- 时间批次轮完在将meterIds论。meterIds就是不断的添加id ， 这个要添加c_meter 中真实有效的ID


## 告警事件

接口： /api/meters/alarmEnventDeviceListPage?BACKTIME=EVENT_ALARM_QUERY

参数

```json
{
    "pageNum": 1,
    "pageSize": 100,
    "conditions": [
        {
            "fieldKey": "last_occurrence_time",
            "fieldType": "Date",
            "operator": "between",
            "values": [
                "2026-07-08 00:00:00",
                "2026-07-17 23:59:59"
            ],
            "fieldUnit": ""
        }
    ]
}
```

- 这个接口其他都不用改变，你只需要改变values中的时间段

## 通讯日志

接口: /api/communication/listPage?BACKTIME=COMMUNICATION_LOG_PAGE

参数: 

```json
{
    "pageNum": 1,
    "pageSize": 100,
    "conditions": [],
    "orderByFields": []
}
```

# 要求

- 首先你要统计曲线，通讯日志，事件三大类中的数据量。 这里我要求统计每个表的数据作为详细数据， 然后在给出三大类的平均数据量， 就是曲线平均有多少，通讯日志平均有多少，事件平均有多少。
- 然后你编写脚本按照上面提供的三类接口去多论不同参去压测。最终得出一份报告
- 这份报告中数据量级别情况说明 ， 三大类接口多轮测试说明

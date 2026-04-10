# Generic Sensor Helpers — Demo

Two minimal recipes showing how to attach a sensor to a SyncField session
without writing a full StreamBase subclass.

## Polling — `polling_serial.py`

Use `PollingSensorStream` when you have a `read()` function the framework
can call on a fixed schedule.

```bash
python examples/generic_sensor_demo/polling_serial.py
```

## Push — `push_async.py`

Use `PushSensorStream` when your data source is callback-driven.

```bash
python examples/generic_sensor_demo/push_async.py
```

Both examples use fake in-memory sources so they run anywhere without hardware.

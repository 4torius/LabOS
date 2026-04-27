# MobileSiLA2Server - Deployment Guide

## Architettura

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         PC ORCHESTRATOR (Windows)                        │
│                                                                          │
│    ┌─────────────────┐         ┌─────────────────┐                      │
│    │   Orchestrator  │◄──────► │   WebApp UI     │ Dropdown: task list  │
│    │     LabCore     │         │                 │                      │
│    └────────┬────────┘         └─────────────────┘                      │
│             │ SiLA2 gRPC                                                │
└─────────────┼───────────────────────────────────────────────────────────┘
              │ port 50053
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     PC ROBOT - Ubuntu (10.16.0.114)                      │
│                     User: inspection@iitreh002lw012u                     │
│                                                                          │
│    ┌─────────────────────────────────────┐                              │
│    │        MobileSiLA2Server            │                              │
│    │                                     │                              │
│    │  Get_AvailableTasks ───────┐        │                              │
│    │       └─ returns task list │        │                              │
│    │                            │        │                              │
│    │  ExecuteTask(task_id) ─────┼──┐     │                              │
│    │       └─ streams progress  │  │     │                              │
│    └────────────────────────────┼──┼─────┘                              │
│                                 │  │                                    │
│    ┌──────────────ROS───────────┼──┼────────────────────────────────┐   │
│    │                            │  │                                │   │
│    │  /setup1/getSubtasksInfo ◄─┘  │  (rosservice call)             │   │
│    │       └─ returns available tasks                               │   │
│    │                               │                                │   │
│    │  /setup1/state_exec ◄─────────┘  (ROS action)                  │   │
│    │       └─ executes task with feedback                           │   │
│    │                                                                │   │
│    │  rpwc_msgs:                                                    │   │
│    │    - executionCommandsAction                                   │   │
│    │    - getSubtaskInfo.srv                                        │   │
│    │    - infoTasks.msg                                             │   │
│    │                                                                │   │
│    └────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

**Il server SiLA2 DEVE girare sul PC Ubuntu** perché deve comunicare con ROS tramite `rospy`.

## Setup sul PC Robot (Ubuntu)

### 1. Trova il catkin workspace con rpwc_msgs

```bash
# Cerca dove si trova rpwc_msgs
find ~/ -name "rpwc_msgs" -type d 2>/dev/null

# Probabilmente è in uno di questi:
# ~/exsensia/catkin_ws/src/rpwc_msgs
# ~/catkin_ws/src/rpwc_msgs
```

### 2. Verifica che rpwc_msgs sia compilato

```bash
# Source ROS
source /opt/ros/noetic/setup.bash

# Source il workspace corretto (adatta il path)
source ~/exsensia/catkin_ws/devel/setup.bash
# oppure
source ~/exsensia/devel/setup.bash

# Verifica
python3 -c "from rpwc_msgs.msg import executionCommandAction; print('OK')"
python3 -c "from rpwc_msgs.srv import getSubtaskInfo; print('OK')"
```

### 3. Se rpwc_msgs non è compilato

```bash
cd ~/exsensia/catkin_ws   # o il tuo catkin workspace
catkin_make
source devel/setup.bash
```

### 4. Copia il server

```bash
# Da Windows (PowerShell), copia il folder:
scp -r ./MobileSiLA2Server inspection@10.16.0.114:~/

# oppure usa rsync:
rsync -avz ./MobileSiLA2Server/ inspection@10.16.0.114:~/MobileSiLA2Server/
```

### 5. Installa dipendenze Python

```bash
ssh inspection@10.16.0.114

cd ~/MobileSiLA2Server
pip3 install --user grpcio grpcio-tools pyyaml zeroconf
```

### 6. Avvia il server

```bash
# Assicurati che ROS sia in esecuzione
roscore &  # se non è già avviato

# Avvia il SiLA2 server
cd ~/MobileSiLA2Server
chmod +x run_server.sh
./run_server.sh
```

## Verifica Connessione ROS

Prima di avviare il server, verifica che i topic ROS siano disponibili:

```bash
# Lista i topic dell'action server
rostopic list | grep state_exec

# Dovrebbe mostrare:
# /setup1/state_exec/cancel
# /setup1/state_exec/feedback
# /setup1/state_exec/goal
# /setup1/state_exec/result
# /setup1/state_exec/status

# Verifica il service (potrebbe essere disponibile solo on-demand)
rosservice list | grep getSubtasksInfo
```

## Configurazione

Il file `config.yaml` contiene:

```yaml
ros:
  # Per server locale (sul PC robot), usa localhost
  master_uri: "http://localhost:11311"
  
  # Namespace dei topic/service
  namespace: "/setup1"
  
  # Path ai task sul PC robot
  task_action:
    tasks_path: "/home/inspection/exsensia/subtask"
```

## Test Connessione dall'Orchestrator

Una volta che il server è in esecuzione sul PC robot:

### Opzione 1: Usa lo script di test (consigliato)

```powershell
# Da Windows PowerShell, nella directory MobileSiLA2Server
cd C:\Users\andre\Desktop\BicoccaLab\v7\SiLA2\MobileSiLA2Server

# Test connessione base
python test_mobile_client.py --host 10.16.0.114

# Esegui un task specifico
python test_mobile_client.py --host 10.16.0.114 --execute kfaawygpdbxasczcahdfasecerefciev
```

### Opzione 2: Script Python diretto

```python
import asyncio
import grpc
from features import TaskManagement_pb2, TaskManagement_pb2_grpc

async def main():
    channel = grpc.aio.insecure_channel("10.16.0.114:50053")
    stub = TaskManagement_pb2_grpc.TaskManagementStub(channel)
    
    # Ottieni i task disponibili (per dropdown)
    response = await stub.Get_AvailableTasks(
        TaskManagement_pb2.Get_AvailableTasks_Request()
    )
    
    print("Task disponibili:")
    for task in response.tasks:
        print(f"  [{task.task_id}] {task.task_name}")
    
    # Esegui un task (con streaming del progresso)
    if response.tasks:
        task_id = response.tasks[0].task_id
        request = TaskManagement_pb2.ExecuteTask_Request(
            task_id=task_id,
            execution_mode="Normal"
        )
        
        print(f"\nEsecuzione task: {task_id}")
        async for msg in stub.ExecuteTask(request):
            if hasattr(msg, 'current_subtask') and msg.current_subtask:
                print(f"  → {msg.current_subtask}: {msg.status}")
            if hasattr(msg, 'success'):
                print(f"  Risultato: {'OK' if msg.success else 'FAIL'}")
    
    await channel.close()

asyncio.run(main())
```

## Troubleshooting

### rpwc_msgs non trovato

```bash
# Verifica che sia nel PYTHONPATH
echo $PYTHONPATH

# Aggiungi manualmente se necessario
export PYTHONPATH=$PYTHONPATH:~/exsensia/catkin_ws/devel/lib/python3/dist-packages
```

### Action server non disponibile

```bash
# Verifica che il nodo robot sia in esecuzione
rosnode list

# Verifica il tipo dell'action
rostopic type /setup1/state_exec/goal
```

### Server SiLA2 non raggiungibile

```bash
# Verifica firewall
sudo ufw status
sudo ufw allow 50053/tcp

# Verifica binding
netstat -tlnp | grep 50053
```

## ROS Message Definitions

### executionCommands.action (rpwc_msgs/executionCommandsAction)

```
# Goal (rpwc_msgs/executionCommandsActionGoal)
string tasksPath        # Path to task definitions
string startTaskID      # Root task ID to execute
string endTaskID        # Optional: last subtask to execute
int16 state             # 0=IDLE, 1=RUNNING, 2=PAUSED, 3=STOPPED, 4=ERROR
int16 mode              # 0=NORMAL, 1=STEP_BY_STEP, 2=DRY_RUN
---
# Result
bool success
string msg
---
# Feedback
string current_subtask
string status
```

Example rostopic pub:
```bash
rostopic pub -1 /setup1/state_exec/goal rpwc_msgs/executionCommandsActionGoal "header:
  seq: 0
  stamp: {secs: 0, nsecs: 0}
  frame_id: ''
goal_id:
  stamp: {secs: 0, nsecs: 0}
  id: ''
goal: {tasksPath: '/home/inspection/exsensia/subtask', startTaskID: 'my_task_id', endTaskID: '', state: 1, mode: 0}"
```

### getSubtaskInfo.srv

```
---
infoTasks[] tasksInfo
bool result
string info
```

### infoTasks.msg

```
string rootId
string rootName
infoSubtask[] subtasksInfo
```

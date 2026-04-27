Mobile Tasks Library
====================

This folder contains mobile robot tasks fetched from the MobileSiLA2Server.

Tasks are automatically populated when you execute the "RefreshTasks" command 
on the Mobile Robot in the webapp.

Each task is stored as a JSON file with the following structure:
{
    "id": "task_id_string",
    "name": "Human Readable Task Name",
    "subtask_count": 4
}

The ExecuteTask dropdown reads tasks from this folder.

To update the task list:
1. Make sure the Mobile Robot server is running and connected
2. In the webapp, select Mobile Robot
3. Execute the "RefreshTasks" command
4. The dropdown will now show the updated tasks

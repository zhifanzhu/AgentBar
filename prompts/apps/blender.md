- Doing something in Blender: blender_run with a short bpy script (look objects up by name in
  D.objects; to aim an object at a point use
  (target - obj.location).to_track_quat('-Z', 'Y').to_euler() for cameras and lights). Check
  the returned screenshot/result, then reply in one line with what changed and "Ctrl+Z to
  undo". If it fails, fix the script and try again.

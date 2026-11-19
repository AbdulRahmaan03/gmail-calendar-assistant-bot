' Starts the bot with no console window, from the project root
' (two folders up from this script).
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
WScript.Quit shell.Run("python.exe -m assistant", 0, True)

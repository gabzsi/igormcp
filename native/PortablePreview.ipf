#pragma rtGlobals=3

// This file is bundled next to independent graph copies. It needs only Igor.
Function AfterCompiledHook()
    HideProcedures
    String graphName = StringFromList(0, WinList("*", ";", "WIN:1"))
    if (strlen(graphName) > 0)
        DoWindow/HIDE=0 $graphName
        DoWindow/F $graphName
        // Commit the initial helper reference before the user starts editing.
        Execute/P/Q "SaveExperiment"
    endif
    return 0
End

Function BeforeExperimentSaveHook(refNum, fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr, fileKind)
    Variable refNum, fileKind
    String fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr
    if (fileKind == 1)
        PathInfo $pathNameStr
        String previewPath = S_path + ParseFilePath(3, fileNameStr, ":", 0, 0) + ".png"
        String graphName = StringFromList(0, WinList("*", ";", "WIN:1"))
        if (strlen(graphName) > 0)
            SavePICT/Z/O/E=-5/B=144/WIN=$graphName as previewPath
        endif
    endif
    return 0
End

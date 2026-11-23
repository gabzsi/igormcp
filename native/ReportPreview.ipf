#pragma rtGlobals=3

// File-open actions must show the saved graph, even if an older copy was hidden.
Function AfterFileOpenHook(refNum, fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr, fileKind)
    Variable refNum, fileKind
    String fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr
    if (fileKind == 1 || fileKind == 2)
        String graphName = StringFromList(0, WinList("*", ";", "WIN:1"))
        if (strlen(graphName) > 0)
            DoWindow/HIDE=0 $graphName
            DoWindow/F $graphName
        endif
    endif
    return 0
End

// Each single-graph experiment is opened with this native Igor save hook.
// Ctrl+S saves the editable PXP and updates its adjacent PNG preview.
Function BeforeExperimentSaveHook(refNum, fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr, fileKind)
    Variable refNum, fileKind
    String fileNameStr, pathNameStr, fileTypeStr, fileCreatorStr

    if (fileKind != 1)
        return 0
    endif
    PathInfo $pathNameStr
    String previewPath = S_path + ParseFilePath(3, fileNameStr, ":", 0, 0) + ".png"
    String graphName = WinName(0, 1)
    if (strlen(graphName) > 0)
        SavePICT/Z/O/E=-5/B=144/WIN=$graphName as previewPath
    endif
    return 0
End

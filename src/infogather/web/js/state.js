(function (global) {
  function createState() {
    return {
      offset: 0,
      total: null,
      cursor: null,
      hasMore: false,
      appliedQuery: "",
      loading: false,
      loadingAppend: false,
      pendingReset: false,
      entriesGeneration: 0,
      treeGeneration: 0,
      selectedSelectors: new Set(),
      favoredOnly: false,
      unnoticedOnly: false,
      updatedWithinDay: false,
      updatedWithinWeek: false,
      versionIs1: false,
      versionIsNot1: false,
      treeGroups: [],
      collapsedGroups: new Set(),
      lastUndo: null,
      mutating: false,
      mutationRefreshTimer: null,
      mutationRefreshNeedsEntries: false,
      renderedFilterSignature: "",
      totalFilterSignature: "",
      renderedFavoredOnly: false,
      renderedUnnoticedOnly: false,
      renderedViewValid: false,
      insPollTimer: null,
      insStatusLoading: false,
      insWasRunning: false
    };
  }

  global.InfoState = {
    createState
  };
})(window);

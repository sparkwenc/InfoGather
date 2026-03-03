(function (global) {
  function createState() {
    return {
      offset: 0,
      total: 0,
      loading: false,
      pendingReset: false,
      selectedSelectors: new Set(),
      favoredOnly: false,
      unnoticedOnly: false,
      updatedWithinDay: false,
      updatedWithinWeek: false,
      versionIs1: false,
      versionIsNot1: false,
      treeGroups: [],
      collapsedGroups: new Set(),
      insPollTimer: null,
      insStatusLoading: false,
      insWasRunning: false
    };
  }

  global.InfoState = {
    createState
  };
})(window);

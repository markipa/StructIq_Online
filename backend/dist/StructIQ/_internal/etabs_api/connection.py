import comtypes.client

def get_active_etabs():
    """
    Connects to the active instance of ETABS.
    Returns the SapModel object if successful, otherwise None.
    """
    try:
        # Initialize COM for the current thread (needed for FastAPI worker threads)
        comtypes.CoInitialize()
        
        # Tries to connect to the currently open ETABS software using the formal Helper approach
        # which is the recommended method for newer versions like ETABS v22
        helper = comtypes.client.CreateObject("ETABSv1.Helper")
        helper = helper.QueryInterface(comtypes.gen.ETABSv1.cHelper)
        
        # Attach to the active ETABS instance
        myETABSObject = helper.GetObject("CSI.ETABS.API.ETABSObject")
        if myETABSObject is None:
            raise Exception("No active instance of ETABS found via Helper.")
            
        SapModel = myETABSObject.SapModel
        return SapModel
    except Exception as e:
        print(f"Error connecting to ETABS via Helper: {e}")
        # Fallback to direct GetActiveObject method
        try:
            print("Falling back to comtypes.client.GetActiveObject...")
            myETABSObject = comtypes.client.GetActiveObject("CSI.ETABS.API.ETABSObject")
            if myETABSObject is None:
                raise Exception("No active instance of ETABS found via comtypes.")
            SapModel = myETABSObject.SapModel
            return SapModel
        except Exception as ex:
            print(f"Fallback Connection error: {ex}")
            return None

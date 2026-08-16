import bpy


class DrawHandlerManager:
    draw_handlers = {}  # Dictionary to track handlers by name

    @classmethod
    def add_handler(cls, name, draw_type, callback):
        """Add a handler with a specific name and callback."""
        if name not in cls.draw_handlers:
            if draw_type == "LINES":
                cls.draw_handlers[name] = bpy.types.SpaceView3D.draw_handler_add(
                    callback, (bpy.context,), 'WINDOW', 'POST_VIEW'
                )
                # print(f"Lines handler '{name}' added with callback: {callback.__name__}")
            if draw_type == "TEXT":
                cls.draw_handlers[name] = bpy.types.SpaceView3D.draw_handler_add(
                    callback, (bpy.context,), 'WINDOW', 'POST_PIXEL'
                )
                # print(f"Text handler '{name}' added with callback: {callback.__name__}")

            return cls.draw_handlers[name]
        else:
            print(f"Handler '{name}' already exists.")

    @classmethod
    def remove_handler(cls, name):
        """Remove a handler by name."""
        if name in cls.draw_handlers:
            bpy.types.SpaceView3D.draw_handler_remove(cls.draw_handlers[name], 'WINDOW')
            del cls.draw_handlers[name]
            # print(f"Handler '{name}' removed.")
        else:
            print(f"No handler found with name '{name}'.")

    @classmethod
    def remove_all_handlers(cls):
        """Remove a handler by name."""
        handlers_tmp = cls.draw_handlers.copy()
        for name in handlers_tmp:
            cls.remove_handler(name)

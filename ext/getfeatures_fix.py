    async def GetFeatures(self, request, context):
        """Get available features and commands."""
        self._ensure_features_loaded()
        
        # Import proto messages for proper serialization
        try:
            import SiLA2Common_pb2 as pb2
        except ImportError:
            from . import SiLA2Common_pb2 as pb2

        features = []
        for f in self._features:
            # Build Command messages with proper Parameter messages
            commands = []
            for cmd in f.commands:
                params = []
                for p in cmd.get('parameters', []):
                    param = pb2.Parameter(
                        identifier=p.get('identifier', ''),
                        display_name=p.get('display_name', ''),
                        description=p.get('description', ''),
                        data_type=p.get('data_type', 'String'),
                        required=p.get('required', True),
                        constraints=p.get('constraints', [])
                    )
                    params.append(param)
                
                responses = []
                for r in cmd.get('responses', []):
                    resp = pb2.Parameter(
                        identifier=r.get('identifier', ''),
                        display_name=r.get('display_name', ''),
                        description=r.get('description', ''),
                        data_type=r.get('data_type', 'String'),
                        required=False
                    )
                    responses.append(resp)
                
                command = pb2.Command(
                    identifier=cmd.get('identifier', ''),
                    display_name=cmd.get('display_name', ''),
                    description=cmd.get('description', ''),
                    observable=cmd.get('observable', False),
                    parameters=params,
                    responses=responses
                )
                commands.append(command)
            
            # Build Property messages
            properties = []
            for prop in f.properties:
                property_msg = pb2.Property(
                    identifier=prop.get('identifier', ''),
                    display_name=prop.get('display_name', ''),
                    description=prop.get('description', ''),
                    data_type=prop.get('data_type', 'String'),
                    observable=prop.get('observable', False)
                )
                properties.append(property_msg)
            
            feature = pb2.Feature(
                identifier=f.identifier,
                display_name=f.display_name,
                description=f.description,
                category=f.category,
                version=f.version,
                commands=commands,
                properties=properties
            )
            features.append(feature)

        return pb2.FeaturesResponse(features=features)

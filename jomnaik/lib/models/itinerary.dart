class Itinerary {
  final int duration;
  final List<Leg> legs;

  Itinerary({required this.duration, required this.legs});

  factory Itinerary.fromJson(Map<String, dynamic> json) {
    var list = json['legs'] as List;
    List<Leg> legList = list.map((i) => Leg.fromJson(i)).toList();
    return Itinerary(
      duration: json['duration'] ?? 0,
      legs: legList,
    );
  }
}

class Leg {
  final String mode;
  final String startTime;
  final String endTime;
  final String? routeShortName;
  final String? headsign;
  final String legGeometryPoints;
  final List<IntermediateStop> intermediateStops; // Add this line

  Leg({
    required this.mode,
    required this.startTime,
    required this.endTime,
    this.routeShortName,
    this.headsign,
    required this.legGeometryPoints,
    required this.intermediateStops, // Add this line
  });

  factory Leg.fromJson(Map<String, dynamic> json) {
    var stopsList = json['intermediateStops'] as List? ?? [];
    List<IntermediateStop> stops = stopsList.map((i) => IntermediateStop.fromJson(i)).toList();

    return Leg(
      mode: json['mode'] ?? 'WALK',
      startTime: json['startTime'] ?? '',
      endTime: json['endTime'] ?? '',
      routeShortName: json['routeShortName'],
      headsign: json['headsign'],
      legGeometryPoints: json['legGeometry']?['points'] ?? '',
      intermediateStops: stops,
    );
  }
}

// Add this new class at the bottom of your model file
class IntermediateStop {
  final String name;
  final double lat;
  final double lon;

  IntermediateStop({required this.name, required this.lat, required this.lon});

  factory IntermediateStop.fromJson(Map<String, dynamic> json) {
    return IntermediateStop(
      name: json['name'] ?? 'Transit Stop',
      lat: json['lat']?.toDouble() ?? 0.0,
      lon: json['lon']?.toDouble() ?? 0.0,
    );
  }
}
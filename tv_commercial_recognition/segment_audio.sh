#!/bin/bash

input_file="$1"
output_directory="$2"

# Default values
noise_level="-100dB"
min_segment_duration="10.0"
max_segment_duration="31.0"
duration="0.8"

# Create the output directory if it doesn't exist
if [ ! -d "$output_directory" ]; then
    mkdir -p "$output_directory"
fi

segment_counter=0
start_time=0

# Process and save the silence periods using input redirection in the while loop
while IFS= read -r line; do
    if [[ $line == *silence_end* ]]; then
        silence_end=$(echo "$line" | cut -d'=' -f 2)
        segment_duration=$(echo "$silence_end - $start_time" | bc -l)
        bound_lower_ok=$(bc <<<"$segment_duration >= $min_segment_duration")
        bound_upper_ok=$(bc <<<"$segment_duration <= $max_segment_duration")

        if [ "$bound_lower_ok" -eq 1 ] && [ "$bound_upper_ok" -eq 1 ]; then
            # Use printf to zero-pad the segment number to 5 digits
            segment_number_padded=$(printf "%05d" "$segment_counter")
            output_file="$output_directory/segment_$segment_number_padded.wav"

            ffmpeg -v warning -i "$input_file" -ss "$start_time" -to "$silence_end" \
                -c copy "$output_file"
        else
            echo "Segment $segment_counter is too short or too long ($segment_duration seconds), skipping.."
        fi

        ((segment_counter++))
        start_time="$silence_end"
    fi
done < <(ffmpeg -v warning -i "$input_file" \
    -af "silencedetect=noise=$noise_level:d=$duration,ametadata=mode=print:file=-" \
    -vn -sn -f s16le -y /dev/null)
